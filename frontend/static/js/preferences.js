"use strict";

// Job preferences: chip inputs, location suggestions, stipend formatting and unsaved-changes status.
// Every field keeps its original name, so the server contract is unchanged and the form still works without scripts.
(() => {
    const form = document.getElementById("settings-form");
    if (!form) return;

    const byId = id => document.getElementById(id);
    const MAX_LOCATIONS = 20, MAX_LOCATION_LENGTH = 100, MAX_LIST_LENGTH = 1000, MAX_STIPEND = 10000000;
    const pending = [];     // report text typed but not yet added
    const submitters = [];  // commit pending text before the form is submitted

    const statusEl = byId("save-status");
    const statusText = statusEl && statusEl.querySelector("span");
    const discard = byId("discard-changes");
    const snapshot = () => new URLSearchParams(new FormData(form)).toString();
    let baseline = "";

    function refresh() {
        if (!statusEl) return;
        const dirty = snapshot() !== baseline || pending.some(check => check());
        statusEl.classList.toggle("is-dirty", dirty);
        statusText.textContent = dirty ? "Unsaved changes" : "No unsaved changes";
        if (discard) discard.hidden = !dirty;
    }

    function makeChip(text, fieldName) {
        const item = document.createElement("li");
        item.className = "pf-chip";
        const label = document.createElement("span");
        label.textContent = text;
        item.append(label);
        if (fieldName) {
            const field = document.createElement("input");
            field.type = "hidden";
            field.name = fieldName;
            field.value = text;
            item.append(field);
        }
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "pf-x";
        remove.setAttribute("aria-label", "Remove " + text);
        remove.textContent = "\u00d7";
        item.append(remove);
        return item;
    }

    /* ---------- Roles ---------- */
    (function roles() {
        const input = byId("roles-input"), chips = byId("roles-chips"), box = byId("roles-box");
        if (!input || !chips || !box) return;
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "roles";
        const values = [];

        function add(raw) {
            for (const part of raw.split(",")) {
                const value = part.trim();
                if (!value || values.some(item => item.toLowerCase() === value.toLowerCase())) continue;
                if (values.concat(value).join(", ").length > MAX_LIST_LENGTH) {
                    input.setCustomValidity("Keep your roles within 1,000 characters.");
                    input.reportValidity();
                    return false;
                }
                values.push(value);
            }
            input.setCustomValidity("");
            return true;
        }
        function render() {
            chips.replaceChildren(...values.map(value => makeChip(value)));
            hidden.value = values.join(", ");
        }
        function commit() {
            if (!input.value.trim()) { input.setCustomValidity(""); return true; }
            const ok = add(input.value);
            if (ok) input.value = "";
            render();
            refresh();
            return ok;
        }

        add(input.value);
        input.value = "";
        input.removeAttribute("name");   // the hidden field carries the comma-separated value
        box.append(hidden);
        render();

        input.addEventListener("keydown", event => {
            if (event.isComposing) return;
            if (event.key === "Enter" || event.key === ",") {
                event.preventDefault();
                commit();
            } else if (event.key === "Backspace" && !input.value && values.length) {
                values.pop();
                render();
                refresh();
            }
        });
        input.addEventListener("input", () => input.setCustomValidity(""));
        input.addEventListener("paste", () => setTimeout(commit));
        input.addEventListener("blur", commit);
        chips.addEventListener("click", event => {
            const button = event.target.closest(".pf-x");
            if (!button) return;
            const index = Array.from(chips.children).indexOf(button.closest("li"));
            if (index < 0) return;
            values.splice(index, 1);
            render();
            refresh();
            input.focus();
        });
        box.addEventListener("click", event => {
            if (event.target === box || event.target === chips) input.focus();
        });
        pending.push(() => input.value.trim() !== "");
        submitters.push(commit);
    })();

    /* ---------- Locations ---------- */
    (function locations() {
        const picker = byId("location-picker");
        if (!picker) return;
        const input = byId("location-input"), box = byId("location-box");
        const addButton = byId("location-add"), selections = byId("selected-locations");
        const status = byId("location-status"), counter = byId("location-count");
        const datalist = byId("location-options");
        const options = datalist ? Array.from(datalist.options, option => ({value: option.value, hint: option.label || ""})) : [];

        const selectedValues = () => Array.from(selections.querySelectorAll('input[name="locations"]'), field => field.value);
        const updateCount = () => { if (counter) counter.textContent = selectedValues().length + "/" + MAX_LOCATIONS; };
        function clearError() { input.setCustomValidity(""); status.textContent = ""; }
        function reject(message) {
            input.setCustomValidity(message);
            status.textContent = message;
            input.reportValidity();
            return false;
        }

        function addEntries(entries) {
            const existing = selectedValues();
            const seen = new Set(existing.map(value => value.toLowerCase()));
            const additions = [];
            for (const value of entries) {
                if (value.length > MAX_LOCATION_LENGTH) return reject("Use locations of at most 100 characters.");
                const key = value.toLowerCase();
                if (!seen.has(key)) { additions.push(value); seen.add(key); }
            }
            if (existing.length + additions.length > MAX_LOCATIONS) return reject("Select at most 20 locations.");
            if (existing.concat(additions).join(",").length > MAX_LIST_LENGTH) {
                return reject("Keep your combined locations within 1,000 characters.");
            }
            for (const value of additions) selections.append(makeChip(value, "locations"));
            input.value = "";
            clearError();
            status.textContent = additions.length ? "Location added. You can enter another." : "That location is already selected.";
            updateCount();
            refresh();
            return true;
        }
        function addLocation() {
            const entries = input.value.split(",").map(value => value.trim()).filter(Boolean);
            if (!entries.length) { clearError(); return true; }
            return addEntries(entries);
        }

        /* Suggestion list (replaces the native datalist popup, keeps the same data). */
        const list = document.createElement("ul");
        list.className = "pf-sug";
        list.id = "location-suggest";
        list.setAttribute("role", "listbox");
        list.setAttribute("aria-label", "Location suggestions");
        list.hidden = true;
        box.append(list);
        input.removeAttribute("list");
        input.setAttribute("role", "combobox");
        input.setAttribute("aria-autocomplete", "list");
        input.setAttribute("aria-controls", "location-suggest");
        input.setAttribute("aria-expanded", "false");
        let matches = [], active = -1;

        function closeList() {
            list.hidden = true;
            active = -1;
            input.setAttribute("aria-expanded", "false");
            input.removeAttribute("aria-activedescendant");
        }
        function highlight(index) {
            active = index;
            Array.from(list.children).forEach((item, position) => item.setAttribute("aria-selected", String(position === index)));
            if (index >= 0) {
                input.setAttribute("aria-activedescendant", "location-option-" + index);
                list.children[index].scrollIntoView({block: "nearest"});
            } else {
                input.removeAttribute("aria-activedescendant");
            }
        }
        function openList() {
            const query = input.value.trim().toLowerCase();
            const chosen = new Set(selectedValues().map(value => value.toLowerCase()));
            matches = options.filter(option => !chosen.has(option.value.toLowerCase()) &&
                (!query || option.value.toLowerCase().includes(query)));
            if (query) {
                matches.sort((a, b) => Number(b.value.toLowerCase().startsWith(query)) - Number(a.value.toLowerCase().startsWith(query)));
            }
            matches = matches.slice(0, query ? 8 : 6);
            active = -1;
            list.replaceChildren(...matches.map((option, index) => {
                const item = document.createElement("li");
                item.id = "location-option-" + index;
                item.setAttribute("role", "option");
                item.setAttribute("aria-selected", "false");
                const name = document.createElement("span");
                name.textContent = option.value;
                item.append(name);
                if (option.hint) {
                    const hint = document.createElement("small");
                    hint.textContent = option.hint;
                    item.append(hint);
                }
                item.addEventListener("mousedown", event => {
                    event.preventDefault();
                    addEntries([option.value]);
                    closeList();
                    input.focus();
                });
                return item;
            }));
            list.hidden = !matches.length;
            input.setAttribute("aria-expanded", String(matches.length > 0));
        }

        addButton.disabled = false;
        for (const button of selections.querySelectorAll("button")) button.disabled = false;
        updateCount();

        input.addEventListener("input", () => { clearError(); openList(); });
        input.addEventListener("focus", openList);
        input.addEventListener("click", () => { if (list.hidden) openList(); });
        input.addEventListener("keydown", event => {
            if (event.isComposing) return;
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (list.hidden) openList();
                if (!matches.length) return;
                const step = event.key === "ArrowDown" ? 1 : -1;
                highlight(active < 0 ? (step > 0 ? 0 : matches.length - 1) : (active + step + matches.length) % matches.length);
            } else if (event.key === "Enter") {
                event.preventDefault();
                if (!list.hidden && active >= 0) addEntries([matches[active].value]); else addLocation();
                closeList();
            } else if (event.key === "Escape" && !list.hidden) {
                event.preventDefault();
                closeList();
            } else if (event.key === "Backspace" && !input.value && selections.lastElementChild) {
                selections.lastElementChild.remove();
                updateCount();
                refresh();
            }
        });
        box.addEventListener("focusout", event => { if (!box.contains(event.relatedTarget)) closeList(); });
        box.addEventListener("click", event => { if (event.target === box) input.focus(); });
        addButton.addEventListener("click", () => { addLocation(); closeList(); input.focus(); });
        selections.addEventListener("click", event => {
            const button = event.target.closest(".pf-x");
            if (!button || !selections.contains(button)) return;
            button.closest("li").remove();
            clearError();
            status.textContent = "Location removed.";
            updateCount();
            refresh();
            input.focus();
        });
        pending.push(() => input.value.trim() !== "");
        submitters.push(addLocation);
    })();

    /* ---------- Minimum stipend ---------- */
    (function stipend() {
        const real = byId("stipend-input"), help = byId("stipend-help"), quick = byId("stipend-quick");
        if (!real) return;
        const defaultHelp = help ? help.textContent : "";
        const format = number => number ? number.toLocaleString("en-IN") : "";
        const shown = document.createElement("input");
        shown.type = "text";
        shown.inputMode = "numeric";
        shown.autocomplete = "off";
        shown.placeholder = "0";
        shown.id = real.id;
        shown.setAttribute("aria-describedby", "stipend-help");
        real.removeAttribute("id");
        real.removeAttribute("placeholder");
        real.type = "hidden";                // the hidden field keeps name="min_stipend" with plain digits
        real.parentElement.insertBefore(shown, real);
        shown.value = format(parseInt(real.value, 10) || 0);

        function sync() {
            for (const button of quick ? quick.querySelectorAll("button") : []) {
                button.setAttribute("aria-pressed", String(real.value === button.dataset.amount));
            }
        }
        function set(number, over) {
            real.value = number ? String(number) : "";
            shown.value = format(number);
            if (help) {
                help.textContent = over ? "Capped at 1 crore (1,00,00,000 INR)." : defaultHelp;
                help.classList.toggle("is-warn", Boolean(over));
            }
            sync();
            refresh();
        }
        shown.addEventListener("input", () => {
            const caret = shown.selectionStart || 0;
            const digitsBefore = shown.value.slice(0, caret).replace(/\D/g, "").length;
            let number = parseInt(shown.value.replace(/\D/g, ""), 10) || 0;
            const over = number > MAX_STIPEND;
            if (over) number = MAX_STIPEND;
            set(number, over);
            let position = 0, seen = 0;
            while (position < shown.value.length && seen < digitsBefore) {
                if (/\d/.test(shown.value[position])) seen += 1;
                position += 1;
            }
            shown.setSelectionRange(position, position);
        });
        if (quick) {
            quick.hidden = false;
            quick.addEventListener("click", event => {
                const button = event.target.closest("button");
                if (!button) return;
                set(real.value === button.dataset.amount ? 0 : Number(button.dataset.amount), false);
            });
        }
        sync();
    })();

    /* ---------- Form-wide behaviour ---------- */
    form.addEventListener("submit", event => {
        for (const commit of submitters) {
            if (!commit()) { event.preventDefault(); return; }
        }
    });
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    if (discard) discard.addEventListener("click", () => location.assign(location.pathname + location.search));
    baseline = snapshot();
    refresh();
})();
