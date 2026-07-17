/*
watchlist.js
============
Wyszukiwanie z debounce (300ms) - czyta lokalny cache przez
GET /settings/watchlist/search, NIGDY nie odpytuje T212 bezpośrednio
(patrz instrument_cache.py po wyjaśnienie dlaczego).

Zakładki kategorii (Akcje/ETF-y/ETP-y z dźwignią/Warranty, patrz common.js::
renderCategoryTabs) - domyślnie otwarte na "Akcje" z pustym query, co teraz
faktycznie przegląda całą zakładkę od A do Z (wcześniej panel "Wszystkie
instrumenty" był pusty dopóki nie wpisałeś czegoś - search_instruments()
zwracała [] dla pustego query bez względu na cokolwiek, patrz fix w
instrument_cache.py).
*/

let searchTimeout = null;
let activeCategory = "stock";
let currentOffset = 0;
let cachedCounts = null;
const PAGE_SIZE = 20;

function renderResults(results, append) {
    const container = document.getElementById("watchlist-results");
    if (!append) container.innerHTML = "";

    if (!append && results.length === 0) {
        container.innerHTML = '<p class="auth-box__hint">Brak wyników.</p>';
        return;
    }

    results.forEach((r) => {
        const row = document.createElement("div");
        row.className = "watchlist-results__item";

        const label = document.createElement("span");
        label.className = "watchlist-results__label";

        const mainLine = document.createElement("span");
        mainLine.textContent = `${r.ticker} — ${r.name}`;
        label.appendChild(mainLine);

        // Rozszerzony opis (typ + waluta) - patrz common.js::instrumentTypeLabel.
        // Odróżnia np. tę samą spółkę notowaną w dwóch walutach na dwóch
        // giełdach (Realty Income USD/EUR) ALBO zupełnie różne, niezależne
        // spółki o podobnej krótkiej nazwie (różni "bottlerzy" Coca-Coli).
        const descLine = document.createElement("span");
        descLine.className = "watchlist-results__desc";
        descLine.textContent = `${instrumentTypeLabel(r.type)} · ${r.currency || "waluta nieznana"}`;
        label.appendChild(descLine);

        row.appendChild(label);

        if (r.is_leveraged) {
            const badge = document.createElement("span");
            badge.className = "badge badge--leverage";
            badge.textContent = "DŹWIGNIA";
            row.appendChild(badge);
        }

        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "watchlist-results__add";
        addBtn.textContent = "Dodaj";
        addBtn.addEventListener("click", () => addToWatchlist(r.ticker));
        row.appendChild(addBtn);
        container.appendChild(row);
    });
}

async function loadResults(append) {
    const query = document.getElementById("watchlist-search").value.trim();
    const offset = append ? currentOffset : 0;
    const loadMoreBtn = document.getElementById("watchlist-load-more");

    try {
        const resp = await fetch(
            `/settings/watchlist/search?q=${encodeURIComponent(query)}&category=${activeCategory}&offset=${offset}`
        );
        const data = await resp.json();
        const results = data.results || [];

        renderResults(results, append);
        currentOffset = offset + results.length;
        loadMoreBtn.style.display = results.length < PAGE_SIZE ? "none" : "";
    } catch (err) {
        console.error("Błąd wyszukiwania:", err);
    }
}

function selectCategory(catId) {
    activeCategory = catId;
    renderCategoryTabs(document.getElementById("watchlist-tabs"), cachedCounts, activeCategory, selectCategory);
    loadResults(false);
}

async function addToWatchlist(ticker) {
    const formData = new URLSearchParams();
    formData.set("ticker", ticker);

    await fetch("/settings/watchlist/add", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formData.toString(),
    });
    window.location.reload();
}

/*
P&L dla ulubionych, które faktycznie posiadasz - JEDNORAZOWO przy wejściu
na stronę (nie auto-odświeżane, żeby nie dokładać kolejnych requestów do
wąskiego rate limitu T212 - patrz warp.js po pełne wyjaśnienie). Dla
tickerów, których nie posiadasz, zostaje puste - appka NIE zgaduje/nie
podaje fałszywej ceny/zmiany, bo T212 API takich danych po prostu nie daje.
*/
async function loadFavoritesPnl() {
    try {
        const resp = await fetch("/warp/account");
        const data = await resp.json();
        if (!data.ok) return;

        const positionsByTicker = {};
        (data.positions || []).forEach((p) => { positionsByTicker[p.ticker] = p; });

        document.querySelectorAll("[data-pnl-for]").forEach((el) => {
            const ticker = el.dataset.pnlFor;
            const position = positionsByTicker[ticker];
            if (!position) return; // brak pozycji - zostaje puste, bez zgadywania

            const ppl = Number(position.ppl);
            const sign = ppl >= 0 ? "+" : "";
            el.textContent = `${position.quantity} szt. · ${sign}${ppl.toFixed(2)}`;
            el.className = "watchlist-favorites__pnl " +
                (ppl >= 0 ? "watchlist-favorites__pnl--profit" : "watchlist-favorites__pnl--loss");
        });
    } catch (err) {
        console.error("Nie udało się pobrać P&L:", err);
    }
}

loadFavoritesPnl();

document.getElementById("watchlist-search").addEventListener("input", () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => loadResults(false), 300);
});

document.getElementById("watchlist-load-more").addEventListener("click", () => loadResults(true));

(async () => {
    cachedCounts = await fetchCategoryCounts();
    renderCategoryTabs(document.getElementById("watchlist-tabs"), cachedCounts, activeCategory, selectCategory);
    loadResults(false);
})();

/*
=== Bulk-fetch logo dla CAŁEJ bazy instrumentów (nie tylko ulubionych) ===
Odpala się w tle po stronie serwera (patrz routes/settings.py::bulk_fetch_logos,
services/logo_cache.py::start_bulk_fetch) - tu tylko pollujemy status co 2s,
dopóki running=True. Jeśli zadanie już leci z poprzedniej wizyty (np.
odświeżyłeś stronę w trakcie), od razu podłączamy się do pollingu zamiast
czekać na klik.
*/
async function pollBulkLogoStatus() {
    const btn = document.getElementById("btn-bulk-fetch-logos");
    const statusEl = document.getElementById("bulk-fetch-status");

    try {
        const resp = await fetch("/settings/logos/bulk-fetch/status");
        const data = await resp.json();

        if (data.running) {
            statusEl.textContent = `Pobieranie w toku: ${data.done}/${data.total}...`;
            btn.disabled = true;
            setTimeout(pollBulkLogoStatus, 2000);
        } else if (data.total > 0) {
            statusEl.textContent = `Gotowe - przetworzono ${data.done}/${data.total} instrumentów.`;
            btn.disabled = false;
        } else {
            btn.disabled = false;
        }
    } catch (err) {
        statusEl.textContent = "Błąd sieci przy sprawdzaniu postępu.";
        btn.disabled = false;
        console.error(err);
    }
}

document.getElementById("btn-bulk-fetch-logos").addEventListener("click", async () => {
    const btn = document.getElementById("btn-bulk-fetch-logos");
    const statusEl = document.getElementById("bulk-fetch-status");
    btn.disabled = true;
    statusEl.textContent = "Uruchamianie...";

    try {
        const resp = await fetch("/settings/logos/bulk-fetch", { method: "POST" });
        const data = await resp.json();
        if (!data.ok) {
            statusEl.textContent = data.error || "Nie udało się uruchomić.";
            btn.disabled = false;
            return;
        }
        pollBulkLogoStatus();
    } catch (err) {
        statusEl.textContent = "Błąd sieci.";
        btn.disabled = false;
        console.error(err);
    }
});

// Podłączenie do trwającego zadania, jeśli już leci (np. po odświeżeniu strony).
pollBulkLogoStatus();
