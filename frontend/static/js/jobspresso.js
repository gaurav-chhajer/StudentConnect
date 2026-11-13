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
        let brewing = false;
        const pour = document.getElementById("pour");
        const beans = `<svg viewBox="0 0 20 28" aria-hidden="true"><ellipse cx="10" cy="14" rx="8" ry="12" fill="currentColor"/><path d="M10 3c-4 8 4 14 0 22" fill="none" stroke="var(--c3)" stroke-width="2"/></svg>`;
        const wave = side => `<div class="${side}"><svg class="wv" viewBox="0 0 1200 60" preserveAspectRatio="none"><path d="M0 30Q150 0 300 30T600 30T900 30T1200 30V60H0Z"/></svg></div>`;
        if (pour) {
            pour.innerHTML = [0, 1, 2].map(() => `<div class="ly">${wave("wt")}<div class="fill"></div>${wave("wb")}</div>`).join("") +
                `<div id="beans"></div><div class="mid"><div class="cupbox"><svg class="bigcup" viewBox="0 0 64 64" fill="none" aria-hidden="true"><path class="st" d="M42 15c-3-4 3-5 0-10M50 15c-3-4 3-5 0-10" stroke="var(--hot)" stroke-width="3" stroke-linecap="round"/><path d="M28 22v16a12 12 0 0 1-12 12h-3M20 18h16" stroke="var(--ink)" stroke-width="9"/><path d="M40 26h14v9a12 12 0 0 1-12 12h-6M54 29h2a5 5 0 0 1 0 10h-2" stroke="var(--ink)" stroke-width="4.5" stroke-linecap="round"/></svg><p class="lbl2" id="pword"></p></div></div>`;
        }
        const setTheme = nextTheme => {
            root.dataset.theme = nextTheme;
            themeButton.setAttribute("aria-pressed", String(nextTheme === "dark"));
            themeButton.setAttribute("aria-label", `Switch to ${nextTheme === "dark" ? "light" : "dark"} mode`);
            try {
                localStorage.setItem("jp-theme", nextTheme);
            } catch (error) {
                if (!(error instanceof DOMException)) throw error;
            }
        };
        themeButton.addEventListener("click", () => {
            const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
            if (!pour || matchMedia("(prefers-reduced-motion: reduce)").matches) {
                setTheme(nextTheme);
                return;
            }
            if (brewing) return;
            brewing = true;
            pour.className = nextTheme === "dark" ? "to-dark" : "to-light";
            document.getElementById("pword").textContent = nextTheme === "dark" ? "Dark roast, coming up" : "Light roast, coming up";
            const beanField = document.getElementById("beans");
            beanField.innerHTML = Array.from({ length: 18 }, (_, index) => {
                const left = (index * 47 + 8) % 96;
                const delay = 0.25 + (index % 7) * 0.11;
                const duration = 1.1 + (index % 5) * 0.16;
                const rotation = 180 + (index * 37) % 540;
                const scale = 0.7 + (index % 4) * 0.15;
                return `<i style="left:${left}%;--d:${delay.toFixed(2)}s;--t:${duration.toFixed(2)}s;--r:${rotation}deg;--s:${scale.toFixed(2)}">${beans}</i>`;
            }).join("");
            themeButton.classList.add("spin");
            void pour.offsetWidth;
            pour.classList.add("go");
            window.setTimeout(() => setTheme(nextTheme), 900);
            window.setTimeout(() => {
                pour.className = "";
                brewing = false;
                themeButton.classList.remove("spin");
            }, 2100);
        });
    }

    const indicator = document.getElementById("ind");
    const primaryNav = document.getElementById("primary-nav");
    function moveIndicator(animate = false) {
        const activeLink = primaryNav?.querySelector("a[aria-current='page']");
        if (!indicator || !primaryNav) return;
        if (!activeLink) {
            indicator.style.opacity = "0";
            return;
        }
        const navRect = primaryNav.getBoundingClientRect();
        const linkRect = activeLink.getBoundingClientRect();
        if (!animate) indicator.style.transition = "none";
        indicator.style.width = `${linkRect.width}px`;
        indicator.style.height = `${linkRect.height}px`;
        indicator.style.transform = `translate(${linkRect.left - navRect.left}px, ${linkRect.top - navRect.top}px)`;
        indicator.style.opacity = "1";
        if (animate) {
            indicator.classList.remove("go");
            void indicator.offsetWidth;
            indicator.classList.add("go");
        } else {
            void indicator.offsetWidth;
            indicator.style.transition = "";
        }
    }
    const previousPage = document.referrer ? new URL(document.referrer) : null;
    moveIndicator(Boolean(previousPage && previousPage.origin === window.location.origin));
    window.addEventListener("resize", () => moveIndicator());

    const boot = document.getElementById("boot");
    if (boot) {
        const bootMessage = boot.querySelector("#bmsg");
        const bootSteps = ["Grinding the beans", "Heating the water", "Brewing your workspace"];
        let bootStep = 0;
        const bootStepTimer = bootMessage ? window.setInterval(() => {
            bootStep = (bootStep + 1) % bootSteps.length;
            bootMessage.textContent = bootSteps[bootStep];
        }, 300) : null;
        window.setTimeout(() => {
            if (bootStepTimer !== null) window.clearInterval(bootStepTimer);
            boot.classList.add("out");
            window.setTimeout(() => boot.remove(), 500);
        }, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 900);
    }
    const offlineScreen = document.getElementById("offline-screen");
    if (offlineScreen) {
        const updateOfflineScreen = () => {
            offlineScreen.hidden = navigator.onLine;
            document.body.classList.toggle("is-offline", !navigator.onLine);
        };
        window.addEventListener("offline", updateOfflineScreen);
        window.addEventListener("online", updateOfflineScreen);
        offlineScreen.querySelector("[data-offline-retry]")?.addEventListener("click", () => location.reload());
        updateOfflineScreen();
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
