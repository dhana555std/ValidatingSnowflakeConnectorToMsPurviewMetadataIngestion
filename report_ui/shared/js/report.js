/* Shared behaviour for every validation report: tab switching and table filtering.
   Wired by data attributes, so templates need no inline handlers:
     <button class="tab-btn" data-tab="tab-id">   shows <div class="tab-pane" id="tab-id">
     <input class="filter" data-target="table-id"> filters the rows of <table id="table-id"> */
(function () {
  function showTab(id) {
    document.querySelectorAll('.tab-pane').forEach(function (p) { p.classList.remove('active'); });
    document.querySelectorAll('.tab-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === id);
    });
    var pane = document.getElementById(id);
    if (pane) pane.classList.add('active');
  }

  function filterRows(input) {
    var q = input.value.toLowerCase();
    document.querySelectorAll('#' + input.dataset.target + ' tbody tr').forEach(function (tr) {
      tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.tab-btn');
    if (btn) showTab(btn.dataset.tab);
  });
  document.addEventListener('input', function (e) {
    if (e.target.classList && e.target.classList.contains('filter')) filterRows(e.target);
  });
})();
