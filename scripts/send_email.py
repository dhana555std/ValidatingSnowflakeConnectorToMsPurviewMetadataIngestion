"""
send_email.py (scripts)
=======================
Sends alert emails through the company SMTP relay for the Purview scan monitor.

Called by scheduler_service.py when a scan check is not a clean pass
(failed, still running, did not run, or check could not reach Purview).
Successful runs never reach this module.

How a send works
    1. The caller queues an email with send_email_async(). It returns at once,
       so the scan check never waits on the mail server.
    2. A background worker sends it through the SMTP relay.
    3. If the send fails, it retries (default 3 attempts, 5 minutes apart).
    4. If every attempt fails, the message is posted to the DevOps Slack webhook.
    5. If the scheduler is stopped during a retry wait, the alert is not sent
       and Slack is not paged, because a shutdown is not a delivery failure.

Settings (.env in the repository root)
    SMTP_HOST                 required, mail relay host name
    SMTP_PORT                 optional, default 587 (STARTTLS used on 587)
    SMTP_USER                 optional, leave empty if the relay needs no login
    SMTP_PASSWORD             optional, leave empty if the relay needs no login
    SMTP_FROM                 required, sender address
    ALERT_TO                  required, recipient(s), comma-separated
    DEVOPS_SLACK_WEBHOOK_URL  optional, Slack fallback after failed emails
    EMAIL_MAX_ATTEMPTS        optional, default 3
    EMAIL_RETRY_SECONDS       optional, default 300 (5 minutes)

Test on its own, from the repository root:
    python scripts\\send_email.py
"""

import os
import smtplib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_ATTEMPTS = int(os.environ.get("EMAIL_MAX_ATTEMPTS", "3"))
RETRY_DELAY_SECONDS = int(os.environ.get("EMAIL_RETRY_SECONDS", "300"))  # 5 minutes

# Small background pool: emails are sent off the caller's thread so scans never wait on the mail server.
# Retries sleep for minutes, so keep a few workers free for other alerts.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="email-sender")
_stopping = threading.Event()


def log(message: str) -> None:
    """Same format as the scheduler's log() so email lines read naturally in its output."""
    print(f"{datetime.now(timezone.utc):%d/%m/%Y %H:%M:%S} UTC  {message}", flush=True)


def _recipients() -> list:
    return [r.strip() for r in os.environ.get("ALERT_TO", "").split(",") if r.strip()]


def _post_to_slack(subject: str, reason: str) -> None:
    """Last resort after all email attempts fail. Never raises."""
    url = os.environ.get("DEVOPS_SLACK_WEBHOOK_URL")
    if not url:
        log("[EMAIL] Slack fallback skipped: DEVOPS_SLACK_WEBHOOK_URL not set")
        return
    try:
        text = (f":warning: Alert email failed after {MAX_ATTEMPTS} attempts.\n"
                f"*{subject}*\nLast error: {reason}")
        resp = requests.post(url, json={"text": text}, timeout=15)
        resp.raise_for_status()
        log("[EMAIL] Slack fallback posted")
    except Exception as exc:
        log(f"[EMAIL] Slack fallback failed: {type(exc).__name__}: {exc}")


def _send_once(subject: str, html_content: str, recipients: list) -> int:
    """One SMTP delivery. Raises on any failure so the retry loop can act."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ["SMTP_FROM"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("This message contains HTML. Open it in an HTML-capable mail client.")
    msg.add_alternative(html_content, subtype="html")

    with smtplib.SMTP(host, port, timeout=30) as server:
        if port == 587:
            server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg, from_addr=sender, to_addrs=recipients)
    return 250


def _send(subject: str, html_content: str, recipients: list):
    """Blocking send with retries; runs on a worker thread. Never raises."""
    if not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_FROM"):
        log("[EMAIL] SMTP_HOST or SMTP_FROM not set, email not sent")
        return None
    if not recipients:
        _post_to_slack(subject, "ALERT_TO is empty")
        return None

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status = _send_once(subject, html_content, recipients)
            log(f"[EMAIL] Sent '{subject}' (SMTP code {status}, attempt {attempt})")
            return status
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log(f"[EMAIL] Attempt {attempt}/{MAX_ATTEMPTS} failed for '{subject}': {last_error}")
        if attempt < MAX_ATTEMPTS:
            # Stopping the scheduler is not a delivery failure, so don't page Slack.
            if _stopping.wait(RETRY_DELAY_SECONDS):
                log("[EMAIL] Shutdown during retry wait, alert not delivered")
                return None

    _post_to_slack(subject, last_error)
    return None


def send_email_async(subject: str, html_content: str, recipients=None):
    """Fire-and-forget: returns a Future immediately, so the next scan can start.

    Recipients default to the comma-separated ALERT_TO in .env.
    """
    return _executor.submit(_send, subject, html_content, recipients or _recipients())


def shutdown_email_sender(wait: bool = True) -> None:
    """Call once at process exit. A retry interrupted by shutdown is not sent and does not page Slack."""
    _stopping.set()
    _executor.shutdown(wait=wait)


if __name__ == "__main__":
    send_email_async(
        "Test alert from send_email.py",
        "<strong>This is a test email.</strong>",
    )
    log("Scan can continue here; email is sending in the background...")
    shutdown_email_sender()  # only so this demo script doesn't exit before sending
