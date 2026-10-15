"use strict";

(() => {
    const controls = document.getElementById("company-search-controls");
    if (!controls) return;

    const input = document.getElementById("company-search");
    const status = document.getElementById("company-search-status");
    const more = document.getElementById("company-view-more");
    const rows = Array.from(document.getElementById("company-options").querySelectorAll("[data-company-name]"));
    const PAGE_SIZE = 12;
    let visibleLimit = PAGE_SIZE;

    function update() {
        const query = input.value.trim().toLowerCase();
        const matches = rows.filter(row => row.dataset.companyName.toLowerCase().includes(query));
        const visible = new Set(matches.slice(0, visibleLimit));
        for (const row of rows) row.hidden = !visible.has(row);
        status.textContent = matches.length
            ? "Showing " + visible.size + " of " + matches.length + " companies."
            : "No companies found. Try another name.";
        more.hidden = matches.length <= PAGE_SIZE;
        more.textContent = visibleLimit >= matches.length ? "View fewer" : "View more";
        more.setAttribute("aria-expanded", String(visibleLimit >= matches.length));
    }

    input.addEventListener("input", () => {
        visibleLimit = PAGE_SIZE;
        update();
    });
    input.addEventListener("keydown", event => {
        if (event.key === "Enter") event.preventDefault();
    });
    more.addEventListener("click", () => {
        const count = rows.filter(row => row.dataset.companyName.toLowerCase().includes(input.value.trim().toLowerCase())).length;
        visibleLimit = visibleLimit >= count ? PAGE_SIZE : visibleLimit + PAGE_SIZE;
        update();
    });
    controls.hidden = false;
    update();
})();
