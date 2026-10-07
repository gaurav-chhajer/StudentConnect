"use strict";

(() => {
    const picker = document.getElementById("location-picker");
    if (!picker) return;

    const input = document.getElementById("location-input");
    const addButton = document.getElementById("location-add");
    const selections = document.getElementById("selected-locations");
    const status = document.getElementById("location-status");
    const form = document.getElementById("settings-form");
    const suggestions = Array.from(document.getElementById("location-options").options);
    const MAX_LOCATIONS = 20;
    const MAX_LOCATION_LENGTH = 100;
    const MAX_PREFERENCES_LENGTH = 1000;

    function selectedValues() {
        return Array.from(selections.querySelectorAll('input[name="locations"]'), field => field.value);
    }

    function clearError() {
        input.setCustomValidity("");
        status.textContent = "";
    }

    function reject(message) {
        input.setCustomValidity(message);
        status.textContent = message;
        input.reportValidity();
        return false;
    }

    function addLocation() {
        const entries = input.value.split(",").map(value => value.trim()).filter(Boolean);
        if (!entries.length) {
            clearError();
            return true;
        }
        const existing = selectedValues();
        const seen = new Set(existing.map(value => value.toLowerCase()));
        const additions = [];
        for (const value of entries) {
            if (value.length > MAX_LOCATION_LENGTH) return reject("Use locations of at most 100 characters.");
            const key = value.toLowerCase();
            if (!seen.has(key)) {
                additions.push(value);
                seen.add(key);
            }
        }
        if (existing.length + additions.length > MAX_LOCATIONS) return reject("Select at most 20 locations.");
        if (existing.concat(additions).join(",").length > MAX_PREFERENCES_LENGTH) {
            return reject("Keep your combined locations within 1,000 characters.");
        }

        for (const value of additions) {
            const item = document.createElement("li");
            item.className = "tag";
            const label = document.createElement("span");
            label.textContent = value;
            const field = document.createElement("input");
            field.type = "hidden";
            field.name = "locations";
            field.value = value;
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "location-remove";
            remove.setAttribute("aria-label", "Remove " + value);
            remove.textContent = "\u00d7";
            item.append(label, field, remove);
            selections.append(item);
        }
        input.value = "";
        clearError();
        status.textContent = additions.length ? "Location added. You can enter another." : "That location is already selected.";
        return true;
    }

    addButton.disabled = false;
    for (const button of selections.querySelectorAll("button")) button.disabled = false;

    input.addEventListener("input", event => {
        clearError();
        // Datalist selections have no dedicated selection event.
        if (!event.isComposing && suggestions.some(option => option.value === input.value.trim())) addLocation();
    });
    input.addEventListener("keydown", event => {
        if (event.key === "Enter" && !event.isComposing) {
            event.preventDefault();
            addLocation();
        }
    });
    addButton.addEventListener("click", () => {
        addLocation();
        input.focus();
    });
    selections.addEventListener("click", event => {
        const button = event.target.closest(".location-remove");
        if (!button || !selections.contains(button)) return;
        button.closest("li").remove();
        clearError();
        status.textContent = "Location removed.";
        input.focus();
    });
    form.addEventListener("submit", event => {
        if (!addLocation()) event.preventDefault();
    });
})();
