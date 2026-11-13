"use strict";

(() => {
    const themeButton = document.getElementById("theme");
    const root = document.documentElement;
    let storedTheme = null;
    try {
        storedTheme = localStorage.getItem("jp-theme");
    } catch (error) {
        if (!(error instanceof DOMException)) throw error;
    }
    const initialTheme = storedTheme === "light" || storedTheme === "dark" ? storedTheme : "dark";
    root.dataset.theme = initialTheme;
    if (themeButton) {
        themeButton.setAttribute("aria-pressed", String(initialTheme === "dark"));
        themeButton.setAttribute("aria-label", `Switch to ${initialTheme === "dark" ? "light" : "dark"} mode`);
        themeButton.addEventListener("click", () => {
            const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
            root.dataset.theme = nextTheme;
            themeButton.setAttribute("aria-pressed", String(nextTheme === "dark"));
            themeButton.setAttribute("aria-label", `Switch to ${nextTheme === "dark" ? "light" : "dark"} mode`);
            try {
                localStorage.setItem("jp-theme", nextTheme);
            } catch (error) {
                if (!(error instanceof DOMException)) throw error;
            }
        });
    }

    document.addEventListener("click", event => {
        const button = event.target instanceof Element ? event.target.closest("[data-password-toggle]") : null;
        if (!button) return;
        const inputId = button.getAttribute("aria-controls");
        const input = inputId ? document.getElementById(inputId) : null;
        if (!(input instanceof HTMLInputElement)) {
            throw new Error("Password visibility control has no matching input.");
        }
        const visible = input.type === "password";
        input.type = visible ? "text" : "password";
        button.textContent = visible ? "Hide" : "Show";
        button.setAttribute("aria-label", `${visible ? "Hide" : "Show"} password`);
    });

    const loader = document.getElementById("page-loader");
    if (!loader) return;
    let pending = false;
    function showLoader() {
        if (!pending) return;
        loader.classList.add("is-active");
        loader.setAttribute("aria-hidden", "false");
    }
    function isLocalNavigation(anchor) {
        if (!anchor || anchor.target || anchor.hasAttribute("download") ||
            anchor.getAttribute("href")?.startsWith("#")) return false;
        try {
            const destination = new URL(anchor.href, location.href);
            return destination.origin === location.origin && !destination.pathname.startsWith("/static/");
        } catch (error) {
            if (error instanceof TypeError) return false;
            throw error;
        }
    }
    document.addEventListener("click", event => {
        const anchor = event.target instanceof Element ? event.target.closest("a") : null;
        if (isLocalNavigation(anchor)) {
            pending = true;
            setTimeout(showLoader, 180);
        }
    });
    document.addEventListener("submit", event => {
        if (event.target instanceof HTMLFormElement && event.target.method.toLowerCase() === "post") {
            pending = true;
            setTimeout(showLoader, 180);
        }
    });
})();
