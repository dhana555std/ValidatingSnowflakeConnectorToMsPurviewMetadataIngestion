"""
region_config
=============
Loads config/regions.json: which databases belong to each region (US NonProd,
EU NonProd, ...). One validation run covers ONE region; scripts/run_validation.py
runs them all, one after the other.
 
config/regions.json
-------------------
{
  "regions": {
    "us": { "label": "US NonProd", "environment": "NonProd", "databases": ["..."] },
    "eu": { "label": "EU NonProd", "environment": "NonProd", "databases": ["..."] }
  }
}
 
Connection settings that differ per region (a separate Snowflake account, a
different Purview root collection, ...) are NOT stored in this file. Put them in
.env with a region suffix; the suffixed value wins, the plain one is the fallback:
 
    SNOWFLAKE_ACCOUNT=xy12345.us-east-1        # used by every region ...
    SNOWFLAKE_ACCOUNT_EU=ab67890.eu-west-1     # ... except EU
 
See get_setting().
"""
 
import json
import os
from typing import Any, Dict, List, Optional
 
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(_ROOT, "config", "regions.json")
 
 
def load_regions(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Return {region_key: {key, label, environment, databases}}.
 
    Returns an empty dict when the file does not exist (region mode is then off
    and the validator behaves as before). Raises ValueError for a malformed file.
    """
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return {}
 
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
 
    regions = raw.get("regions") if isinstance(raw, dict) else None
    if not isinstance(regions, dict) or not regions:
        raise ValueError(f'{path}: expected a non-empty "regions" object')
 
    out: Dict[str, Dict[str, Any]] = {}
    for name, cfg in regions.items():
        dbs = cfg.get("databases") if isinstance(cfg, dict) else None
        if not isinstance(dbs, list) or not all(isinstance(d, str) for d in dbs):
            raise ValueError(f'{path}: region "{name}" needs a "databases" list of names')
 
        seen, cleaned = set(), []                      # de-duplicate, keep order
        for d in dbs:
            d = d.strip()
            if d and d.upper() not in seen:
                seen.add(d.upper())
                cleaned.append(d)
        if not cleaned:
            raise ValueError(f'{path}: region "{name}" has an empty "databases" list')
 
        key = name.strip().lower()
        out[key] = {
            "key":         key,
            "label":       cfg.get("label") or name.upper(),
            "environment": cfg.get("environment"),
            "databases":   cleaned,
        }
    return out
 
 
def select_region(regions: Dict[str, Dict[str, Any]], requested: Optional[str]) -> Optional[str]:
    """Pick the region key for this run.
 
    * no regions configured        -> None (region mode off)
    * a region was requested       -> that key (ValueError if unknown)
    * exactly one region exists    -> that one
    * several exist, none requested-> None (caller must ask for --region)
    """
    if not regions:
        return None
    if requested and requested.strip():
        key = requested.strip().lower()
        if key not in regions:
            raise ValueError(
                f"Unknown region '{requested}'. Available: {', '.join(sorted(regions))}"
            )
        return key
    if len(regions) == 1:
        return next(iter(regions))
    return None
 
 
def get_setting(name: str, region: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
    """Environment value for *name*, preferring NAME_<REGION> over plain NAME."""
    if region:
        value = os.environ.get(f"{name}_{region.upper()}")
        if value:
            return value
    return os.environ.get(name) or default
 