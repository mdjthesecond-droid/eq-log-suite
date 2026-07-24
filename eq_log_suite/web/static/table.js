function makeSortable(table) {
  const headers = table.querySelectorAll("thead th");
  headers.forEach((th, idx) => {
    th.addEventListener("click", () => {
      const tbody = table.querySelector("tbody");
      const rows = Array.from(tbody.querySelectorAll("tr"));
      const asc = th.dataset.sortDir !== "asc";
      headers.forEach((h) => delete h.dataset.sortDir);
      th.dataset.sortDir = asc ? "asc" : "desc";
      rows.sort((a, b) => {
        const av = a.children[idx].textContent.trim();
        const bv = b.children[idx].textContent.trim();
        const an = parseFloat(av);
        const bn = parseFloat(bv);
        let cmp;
        if (!isNaN(an) && !isNaN(bn) && av !== "" && bv !== "") {
          cmp = an - bn;
        } else {
          cmp = av.localeCompare(bv);
        }
        return asc ? cmp : -cmp;
      });
      rows.forEach((r) => tbody.appendChild(r));
    });
  });
}

function makeFilterable(input, table) {
  input.addEventListener("input", () => {
    const q = input.value.toLowerCase();
    table.querySelectorAll("tbody tr").forEach((row) => {
      row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("table.sortable").forEach(makeSortable);
  document.querySelectorAll("[data-filter-for]").forEach((input) => {
    const table = document.getElementById(input.dataset.filterFor);
    if (table) makeFilterable(input, table);
  });
});
