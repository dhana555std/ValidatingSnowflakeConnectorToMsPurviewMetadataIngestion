/* Shared behaviour for every validation report: tab switching and table filtering.
   Wired by data attributes, so templates need no inline handlers:
     <button class="tab-btn" data-tab="tab-id"> shows <div class="tab-pane" id="tab-id">
     <input class="filter" data-target="table-id"> filters rows of <table id="table-id">

   The Purview scan table with id="t-scans" also supports ascending and
   descending sorting on every column header.
*/
(function () {
  "use strict";

  function showTab(id) {
    document.querySelectorAll(".tab-pane").forEach(function (pane) {
      pane.classList.remove("active");
    });

    document.querySelectorAll(".tab-btn").forEach(function (button) {
      button.classList.toggle("active", button.dataset.tab === id);
    });

    var pane = document.getElementById(id);
    if (pane) {
      pane.classList.add("active");
    }
  }

  function filterRows(input) {
    var query = input.value.toLowerCase();
    var target = document.getElementById(input.dataset.target);

    if (!target) {
      return;
    }

    target.querySelectorAll("tbody tr").forEach(function (row) {
      row.style.display = row.textContent.toLowerCase().includes(query)
        ? ""
        : "none";
    });
  }

  function getCellText(cell) {
    return cell ? cell.textContent.trim() : "";
  }

  function parseUtcDate(value) {
    var match = value.match(
      /^(\d{2})\/(\d{2})\/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})/
    );

    if (!match) {
      return null;
    }

    return Date.UTC(
      Number(match[3]),
      Number(match[2]) - 1,
      Number(match[1]),
      Number(match[4]),
      Number(match[5]),
      Number(match[6])
    );
  }

  function parseScheduleDetail(value) {
    var intervalFrequency = value.match(
      /Every\s+(\d+)\s+([A-Za-z]+)/i
    );

    if (!intervalFrequency) {
      return null;
    }

    var monthDay = value.match(/on\s+day\s+(\d+)/i);
    var scheduleTime = value.match(/at\s+(\d{1,2}):(\d{2})/i);
    var frequencyOrder = {
      minute: 1,
      hour: 2,
      day: 3,
      week: 4,
      month: 5,
      year: 6
    };
    var frequency = intervalFrequency[2]
      .toLowerCase()
      .replace(/s$/, "");

    return [
      frequencyOrder[frequency] || 99,
      Number(intervalFrequency[1]),
      monthDay ? Number(monthDay[1]) : 0,
      scheduleTime ? Number(scheduleTime[1]) : 0,
      scheduleTime ? Number(scheduleTime[2]) : 0
    ];
  }

  function compareArrays(first, second) {
    var length = Math.max(first.length, second.length);

    for (var index = 0; index < length; index += 1) {
      var difference =
        Number(first[index] || 0) - Number(second[index] || 0);

      if (difference !== 0) {
        return difference;
      }
    }

    return 0;
  }

  function compareValues(firstValue, secondValue, columnIndex) {
    var firstIsEmpty = firstValue === "";
    var secondIsEmpty = secondValue === "";

    if (firstIsEmpty || secondIsEmpty) {
      if (firstIsEmpty === secondIsEmpty) {
        return 0;
      }

      return firstIsEmpty ? 1 : -1;
    }

    /* Last Scan Time */
    if (columnIndex === 4) {
      var firstDate = parseUtcDate(firstValue);
      var secondDate = parseUtcDate(secondValue);

      if (firstDate !== null && secondDate !== null) {
        return firstDate - secondDate;
      }
    }

    /* Schedule Detail */
    if (columnIndex === 7) {
      var firstSchedule = parseScheduleDetail(firstValue);
      var secondSchedule = parseScheduleDetail(secondValue);

      if (firstSchedule && secondSchedule) {
        return compareArrays(firstSchedule, secondSchedule);
      }
    }

    /* Runs and status-count columns */
    if (columnIndex >= 8) {
      var firstNumber = Number(firstValue.replace(/,/g, ""));
      var secondNumber = Number(secondValue.replace(/,/g, ""));

      if (!Number.isNaN(firstNumber) && !Number.isNaN(secondNumber)) {
        return firstNumber - secondNumber;
      }
    }

    return firstValue.localeCompare(secondValue, undefined, {
      numeric: true,
      sensitivity: "base"
    });
  }

  function enableScansTableSorting() {
    var table = document.getElementById("t-scans");

    if (!table || !table.tHead || table.tBodies.length === 0) {
      return;
    }

    var headerRow = table.tHead.rows[0];

    if (!headerRow) {
      return;
    }

    var headers = Array.from(headerRow.cells);
    var tableBody = table.tBodies[0];

    headers.forEach(function (header, columnIndex) {
      if (header.dataset.sortingEnabled === "true") {
        return;
      }

      header.dataset.sortingEnabled = "true";
      header.classList.add("sortable-column");
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-sort", "none");
      header.title = "Click to sort this column";

      function sortColumn() {
        var ascending = !header.classList.contains("sort-ascending");

        headers.forEach(function (currentHeader) {
          currentHeader.classList.remove(
            "sort-ascending",
            "sort-descending"
          );
          currentHeader.setAttribute("aria-sort", "none");
        });

        header.classList.add(
          ascending ? "sort-ascending" : "sort-descending"
        );
        header.setAttribute(
          "aria-sort",
          ascending ? "ascending" : "descending"
        );

        var rows = Array.from(tableBody.rows);

        rows.sort(function (firstRow, secondRow) {
          var comparison = compareValues(
            getCellText(firstRow.cells[columnIndex]),
            getCellText(secondRow.cells[columnIndex]),
            columnIndex
          );

          return ascending ? comparison : -comparison;
        });

        rows.forEach(function (row) {
          tableBody.appendChild(row);
        });
      }

      header.addEventListener("click", sortColumn);
      header.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sortColumn();
        }
      });
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest(".tab-btn");

    if (button) {
      showTab(button.dataset.tab);
    }
  });

  document.addEventListener("input", function (event) {
    if (
      event.target.classList &&
      event.target.classList.contains("filter")
    ) {
      filterRows(event.target);
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener(
      "DOMContentLoaded",
      enableScansTableSorting
    );
  } else {
    enableScansTableSorting();
  }
}());
