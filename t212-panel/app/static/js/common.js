/*
common.js
=========
Wspólne narzędzia UI ładowane na KAŻDEJ stronie (przez base.html):
- confirmDialog(message) - modal potwierdzenia zamiast brzydkiego window.confirm()
- setConnDot(status) - aktualizacja kropki statusu połączenia z T212 w topbarze
- playSuccess()/playError() - dźwięki (Web Audio, bez plików mp3)

Trzymane osobno od warp.js, bo modal i kropka żyją w base.html (a więc
dotyczą też stron poza Warp Mode - np. Smart Virtual Pie w pie.js, które
reużywa też playSuccess/playError zamiast duplikować logikę dźwięku).
*/

/*
avatarHue(ticker) - JS odpowiednik utils.py::avatar_hue(), ten sam wzór
(suma kodów znaków % 360), żeby kolor awatara fallback dla tego samego
tickera ZAWSZE wychodził identyczny czy liczony po stronie serwera
(Jinja, przy renderze strony) czy tutaj (JS, przy dorenderowaniu wyników
wyszukiwania bez przeładowania strony).
*/
function avatarHue(ticker) {
    let sum = 0;
    for (let i = 0; i < ticker.length; i++) sum += ticker.charCodeAt(i);
    return sum % 360;
}

/*
Dźwięki - bez zewnętrznych plików audio (error.mp3/success.mp3 z pierwotnego
pomysłu Gemini), generowane na żywo przez Web Audio API (prosty oscylator),
żeby appka nie zależała od dodatkowych plików binarnych do wgrania na NAS.
Przeniesione tu z warp.js, bo pie.js (Smart Virtual Pie) też ich potrzebuje.
*/
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

function playTone(freq, durationMs) {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = freq;
    osc.type = "square";
    gain.gain.setValueAtTime(0.05, audioCtx.currentTime);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + durationMs / 1000);
}

function playSuccess() { playTone(880, 90); }
function playError() { playTone(160, 180); }

function confirmDialog(message) {
    return new Promise((resolve) => {
        const overlay = document.getElementById("confirm-modal");
        const messageEl = document.getElementById("confirm-modal-message");
        const yesBtn = document.getElementById("confirm-modal-yes");
        const noBtn = document.getElementById("confirm-modal-no");

        messageEl.textContent = message;
        overlay.classList.remove("modal-overlay--hidden");

        // Klonowanie przycisków żeby pozbyć się poprzednich listenerów -
        // prostsze niż ręczne removeEventListener przy powtórnym wywołaniu.
        const newYes = yesBtn.cloneNode(true);
        const newNo = noBtn.cloneNode(true);
        yesBtn.replaceWith(newYes);
        noBtn.replaceWith(newNo);

        function close(result) {
            overlay.classList.add("modal-overlay--hidden");
            resolve(result);
        }

        newYes.addEventListener("click", () => close(true));
        newNo.addEventListener("click", () => close(false));
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) close(false);
        }, { once: true });
    });
}

/*
Komunikaty flash (patrz base.html) znikaja same po kilku sekundach zamiast
czekac w nieskonczonosc na kolejna akcje/przeladowanie strony.
*/
document.querySelectorAll(".flash-bar__msg").forEach((msg) => {
    setTimeout(() => {
        msg.classList.add("flash-bar__msg--fade");
        setTimeout(() => msg.remove(), 400);
    }, 4500);
});

/*
=== Przechwytywanie błędów JS (do automatycznego dołączania w zgłoszeniach) ===
Prosty ring buffer - trzyma ostatnie 10 błędów z tej sesji przeglądarki
(nie zapisywane nigdzie trwale, znikają przy odświeżeniu strony - to
wystarczy, bo user zwykle zgłasza problem od razu po tym jak się wydarzył).
*/
const _errorLog = [];
const MAX_ERROR_LOG = 10;

function _recordError(message) {
    const timestamp = new Date().toISOString().split("T")[1].split(".")[0];
    _errorLog.push(`[${timestamp}] ${message}`);
    if (_errorLog.length > MAX_ERROR_LOG) _errorLog.shift();
}

window.addEventListener("error", (event) => {
    _recordError(`${event.message} (${event.filename}:${event.lineno})`);
});
window.addEventListener("unhandledrejection", (event) => {
    _recordError(`Unhandled promise rejection: ${event.reason}`);
});

function collectErrorLog() {
    return _errorLog.join("\n");
}

/*
=== Modal "Zgłoś problem" ===
*/
document.addEventListener("DOMContentLoaded", () => {
    const reportBtn = document.getElementById("report-problem-btn");
    const reportModal = document.getElementById("report-modal");
    if (!reportBtn || !reportModal) return; // niezalogowany - modal nie istnieje w DOM

    const form = document.getElementById("report-form");
    const statusEl = document.getElementById("report-form-status");
    const cancelBtn = document.getElementById("report-modal-cancel");

    function closeReportModal() {
        reportModal.classList.add("modal-overlay--hidden");
        statusEl.textContent = "";
    }

    reportBtn.addEventListener("click", () => {
        reportModal.classList.remove("modal-overlay--hidden");
    });
    cancelBtn.addEventListener("click", closeReportModal);
    reportModal.addEventListener("click", (e) => {
        if (e.target === reportModal) closeReportModal();
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const submitBtn = form.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        statusEl.textContent = "Wysyłanie…";

        const formData = new FormData(form);
        formData.set("page_url", window.location.href);
        formData.set("browser_errors", collectErrorLog());

        try {
            const resp = await fetch("/report/submit", { method: "POST", body: formData });
            const data = await resp.json();

            if (data.ok) {
                statusEl.textContent = "Wysłano. Dzięki!";
                setTimeout(() => {
                    closeReportModal();
                    form.reset();
                }, 1200);
            } else {
                statusEl.textContent = `Błąd: ${data.error ?? "nieznany"}`;
            }
        } catch (err) {
            statusEl.textContent = "Błąd sieci przy wysyłaniu.";
            console.error(err);
        } finally {
            submitBtn.disabled = false;
        }
    });
});

/*
Przełącznik motywu - optymistyczna zmiana w UI od razu po kliknięciu
(bez czekania na odpowiedź serwera), zapisywana asynchronicznie w tle
przez POST /settings/theme. Jeśli request się nie uda, motyw i tak
zostaje zmieniony lokalnie na czas tej wizyty - kolejne odświeżenie
strony wróci do wartości zapisanej w bazie.
*/
document.addEventListener("DOMContentLoaded", () => {
    const toggleBtn = document.getElementById("theme-toggle");
    if (!toggleBtn) return;

    toggleBtn.addEventListener("click", async () => {
        const html = document.documentElement;
        const newTheme = html.getAttribute("data-theme") === "light" ? "dark" : "light";
        html.setAttribute("data-theme", newTheme);

        try {
            await fetch("/settings/theme", { method: "POST" });
        } catch (err) {
            console.error("Nie udało się zapisać motywu:", err);
        }
    });
});

/*
setConnDot(status) - status: "ok" | "error" | "unknown"
"unknown" to stan początkowy (jeszcze nie sprawdzaliśmy) - szary, nie
czerwony, żeby nie sugerować fałszywie że coś już nie działa.
*/
function setConnDot(status) {
    const dot = document.querySelector("#t212-status-dot .topbar__conn-dot");
    const wrapper = document.getElementById("t212-status-dot");
    if (!dot || !wrapper) return;

    dot.classList.remove("topbar__conn-dot--ok", "topbar__conn-dot--error", "topbar__conn-dot--unknown");

    const labels = {
        ok: "Połączenie z T212: OK",
        error: "Połączenie z T212: BŁĄD",
        unknown: "Status połączenia z T212 (nieznany)",
    };
    dot.classList.add(`topbar__conn-dot--${status}`);
    wrapper.title = labels[status] || labels.unknown;
}

/*
=== Zakładki kategorii instrumentów (Akcje USD / Akcje EUR / Pozostałe) ===
Współdzielone między watchlist.js (panel "Wszystkie instrumenty"), pie.js
(wyszukiwarka "Dodaj aktywo" w Smart Virtual Pie) i bot.js - wszystkie czytają
ten sam lokalny cache instrumentów (services/instrument_cache.py), więc ten
sam zestaw zakładek i te same liczniki mają sens wszędzie. Podział WALUTOWY
akcji (poprawione 18.07.2026 - pierwsza wersja scaliła akcje w jedną
zakładkę, ale Adamowi chodziło o rozróżnienie USD/EUR, nie akcje/nie-akcje).
"Pozostałe" to worek na wszystko inne: ETF, ETF z dźwignią, Warrant, akcje w
innych walutach (GBX/CAD/CHF itd.). Plakietka "DŹWIGNIA" przy pojedynczych
wynikach (Instrument.is_leveraged) zostaje bez zmian - to ostrzeżenie
per-instrument, nie kategoria zakładki.
*/
const INSTRUMENT_CATEGORIES = [
    { id: "stock_usd", label: "Akcje USD" },
    { id: "stock_eur", label: "Akcje EUR" },
    { id: "other", label: "Pozostałe" },
];

/*
instrumentTypeLabel(type) - "STOCK"/"ETF"/"WARRANT" (surowe pole z T212,
patrz Instrument.instrument_type) -> polska etykieta do wyników wyszukiwania.
Współdzielone między watchlist.js/pie.js/bot.js - patrz "rozszerzony opis"
w renderResults/renderAssetResults/renderBotAssetResults, dodany bo kilka
zupełnie ODDZIELNYCH spółek/notowań potrafi mieć identyczną krótką nazwę
(np. "Coca-Cola" - kilka niezależnych spółek-bottlerów, albo ta sama spółka
notowana w dwóch walutach na dwóch giełdach) - sam ticker+nazwa czasem nie
wystarczał, żeby je odróżnić.
*/
function instrumentTypeLabel(type) {
    const labels = { STOCK: "Akcja", ETF: "ETF", WARRANT: "Warrant" };
    return labels[type] || type || "?";
}

function renderCategoryTabs(container, counts, activeCategory, onSelect) {
    container.innerHTML = "";
    INSTRUMENT_CATEGORIES.forEach((cat) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tab" +
            (cat.id === activeCategory ? " tab--active" : "") +
            (cat.risk ? " tab--risk" : "");
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", cat.id === activeCategory ? "true" : "false");

        const count = counts ? counts[cat.id] : undefined;
        btn.innerHTML = count !== undefined
            ? `${cat.label} <span class="tab__count">${count}</span>`
            : cat.label;

        btn.addEventListener("click", () => onSelect(cat.id));
        container.appendChild(btn);
    });
}

async function fetchCategoryCounts() {
    try {
        const resp = await fetch("/settings/watchlist/category-counts");
        const data = await resp.json();
        return data.ok ? data.counts : null;
    } catch (err) {
        console.error("Nie udało się pobrać liczników kategorii:", err);
        return null;
    }
}
