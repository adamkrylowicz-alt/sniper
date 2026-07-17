/*
pie.js
======
Obsługa widoku Smart Virtual Pie (jednego koszyka). 3-way sync: suwak wagi /
kwota / ilość przeliczają się wzajemnie NA ŻYWO (client-side), ale zapis
wagi do bazy (POST /pie/asset/<id>/weight) leci dopiero na 'change' (puszczenie
suwaka / opuszczenie pola), nie na każdy 'input' - ta sama dyscyplina co
debounce wyszukiwarki w watchlist.js i throttle w warp.js.

Wzory (per wiersz aktywa):
  weightSum = suma target_weight ze WSZYSTKICH wierszy aktualnie w DOM
  amount    = budget * (waga_wiersza / weightSum)
  quantity  = price > 0 ? amount / price : —

Edycja kwoty/ilości wprost przelicza wagę tego wiersza W DRUGĄ STRONĘ
(amount -> weight), żeby pola zawsze były spójne niezależnie od tego,
które z nich user ruszył jako ostatnie.
*/

function pieId() {
    return document.querySelector(".pie-detail").dataset.pieId;
}

function weightSum() {
    let sum = 0;
    document.querySelectorAll(".pie-asset-row").forEach((row) => {
        sum += Number(row.querySelector("[data-weight-slider]").value) || 0;
    });
    return sum;
}

function getBudget() {
    const input = document.getElementById("pie-budget");
    return input ? Number(input.value) || 0 : 0;
}

function refreshAllPct() {
    const sum = weightSum();
    document.querySelectorAll(".pie-asset-row").forEach((row) => {
        const w = Number(row.querySelector("[data-weight-slider]").value) || 0;
        const pct = sum > 0 ? (w / sum) * 100 : 0;
        row.querySelector("[data-weight-pct]").textContent = `${pct.toFixed(1)}%`;
    });
    updateDonut();
}

/*
Wykres kolowy (donut) alokacji koszyka - conic-gradient zamiast SVG/biblioteki
(zero zaleznosci, latwe do przeliczenia na kazdej zmianie wagi). Kolor
kazdego segmentu = ten sam hsl() co awatar danego wiersza (patrz
pie_detail.html), wiec kolory w wykresie i na liscie ponizej sie zgadzaja -
lista pelni role legendy, bez potrzeby osobnego komponentu.
*/
function updateDonut() {
    const donut = document.getElementById("pie-donut");
    if (!donut) return;

    const sum = weightSum();
    const rows = document.querySelectorAll(".pie-asset-row");
    if (sum <= 0 || rows.length === 0) {
        donut.style.background = "var(--hairline)";
        return;
    }

    let angle = 0;
    const stops = [];
    rows.forEach((row) => {
        const w = Number(row.querySelector("[data-weight-slider]").value) || 0;
        const color = row.querySelector(".pie-asset-row__avatar").style.background || "#888";
        const deg = (w / sum) * 360;
        stops.push(`${color} ${angle.toFixed(2)}deg ${(angle + deg).toFixed(2)}deg`);
        angle += deg;
    });
    donut.style.background = `conic-gradient(${stops.join(", ")})`;
}

function recomputeAmountQty(row) {
    const sum = weightSum();
    const budget = getBudget();
    const w = Number(row.querySelector("[data-weight-slider]").value) || 0;
    const price = Number(row.querySelector("[data-price]").value) || 0;

    const amount = sum > 0 ? budget * (w / sum) : 0;
    row.querySelector("[data-amount]").value = amount ? amount.toFixed(2) : "";
    if (price > 0) {
        row.querySelector("[data-qty]").value = (amount / price).toFixed(4);
    }
}

function onSliderInput(row) {
    recomputeAmountQty(row);
    refreshAllPct();
}

function onBudgetInput() {
    document.querySelectorAll(".pie-asset-row").forEach(recomputeAmountQty);
    refreshAllPct();
}

function onAmountInput(row) {
    const budget = getBudget();
    const sum = weightSum();
    const amount = Number(row.querySelector("[data-amount]").value) || 0;
    const price = Number(row.querySelector("[data-price]").value) || 0;

    if (budget > 0 && sum > 0) {
        row.querySelector("[data-weight-slider]").value = ((amount / budget) * sum).toFixed(2);
    }
    if (price > 0) {
        row.querySelector("[data-qty]").value = (amount / price).toFixed(4);
    }
    refreshAllPct();
}

function onQuantityInput(row) {
    const budget = getBudget();
    const sum = weightSum();
    const qty = Number(row.querySelector("[data-qty]").value) || 0;
    const price = Number(row.querySelector("[data-price]").value) || 0;
    const amount = qty * price;

    row.querySelector("[data-amount]").value = amount ? amount.toFixed(2) : "";
    if (budget > 0 && sum > 0) {
        row.querySelector("[data-weight-slider]").value = ((amount / budget) * sum).toFixed(2);
    }
    refreshAllPct();
}

async function saveWeight(row) {
    const assetId = row.dataset.assetId;
    const weight = row.querySelector("[data-weight-slider]").value;
    try {
        await fetch(`/pie/asset/${assetId}/weight`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target_weight: weight }),
        });
    } catch (err) {
        console.error("Nie udało się zapisać wagi:", err);
    }
}

/*
=== Mini-wykresy (Finnhub, przez /pie/<id>/charts) ===
Jedno wywołanie na cały widok, nie per-wiersz - patrz price_feed.py.
*/
function renderSparkline(svg, closes) {
    if (!closes || closes.length < 2) {
        svg.innerHTML = "";
        return;
    }
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;
    const stepX = 100 / (closes.length - 1);
    const points = closes
        .map((c, i) => `${(i * stepX).toFixed(1)},${(29 - ((c - min) / range) * 28).toFixed(1)}`)
        .join(" ");
    const cls = closes[closes.length - 1] >= closes[0]
        ? "pie-asset-row__spark-line--up"
        : "pie-asset-row__spark-line--down";
    svg.innerHTML = `<polyline class="${cls}" points="${points}" fill="none" stroke-width="1.5"/>`;
}

async function loadCharts() {
    try {
        const resp = await fetch(`/pie/${pieId()}/charts`);
        const data = await resp.json();
        if (!data.ok) return;

        document.querySelectorAll(".pie-asset-row").forEach((row) => {
            const closes = data.charts[row.dataset.ticker];
            renderSparkline(row.querySelector("[data-spark]"), closes);

            const priceInput = row.querySelector("[data-price]");
            if (!priceInput.value && closes && closes.length) {
                priceInput.value = closes[closes.length - 1];
            }
        });
    } catch (err) {
        console.error("Nie udało się pobrać wykresów:", err);
    }
}

/*
=== Kupno - ten sam próg potwierdzenia 70% Hard Cap co warp.js, ten sam
    _guard (współdzielony z Warp Mode w scalping.py), więc limit z
    /warp/limits jest poprawny też tutaj. ===
*/
let cachedMaxOrderValue = null;
const CONFIRM_THRESHOLD_RATIO = 0.7;

async function loadLimits() {
    try {
        const resp = await fetch("/warp/limits");
        const data = await resp.json();
        cachedMaxOrderValue = data.max_order_value ? Number(data.max_order_value) : null;
    } catch (err) {
        console.error("Nie udało się pobrać limitów:", err);
    }
}

async function buyAsset(row) {
    const assetId = row.dataset.assetId;
    const ticker = row.dataset.ticker;
    const qty = row.querySelector("[data-qty]").value;
    const price = row.querySelector("[data-price]").value || null;
    const statusEl = row.querySelector("[data-status]");
    const buyBtn = row.querySelector("[data-buy]");

    if (!qty || Number(qty) <= 0) {
        statusEl.textContent = "Podaj ilość > 0";
        return;
    }

    if (price && cachedMaxOrderValue) {
        const estValue = Number(qty) * Number(price);
        if (estValue >= cachedMaxOrderValue * CONFIRM_THRESHOLD_RATIO) {
            const confirmed = await confirmDialog(
                `Duże zlecenie: KUP ${qty} × ${ticker} ` +
                `≈ ${estValue.toFixed(2)} (limit: ${cachedMaxOrderValue.toFixed(2)}). Kontynuować?`
            );
            if (!confirmed) return;
        }
    }

    buyBtn.disabled = true;
    statusEl.textContent = "wysyłanie…";

    try {
        const resp = await fetch(`/pie/asset/${assetId}/buy`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ quantity: qty, estimated_price: price }),
        });
        const data = await resp.json();

        if (data.ok) {
            playSuccess();
            statusEl.textContent = `OK #${data.order_id ?? "?"}`;
        } else if (data.blocked) {
            playError();
            statusEl.textContent = `ZABLOKOWANE: ${data.reason ?? data.decision}`;
        } else {
            playError();
            statusEl.textContent = `BŁĄD: ${data.error ?? "nieznany"}`;
        }
    } catch (err) {
        playError();
        statusEl.textContent = "BŁĄD SIECI";
        console.error(err);
    } finally {
        buyBtn.disabled = false;
    }
}

/*
=== Smart FX Guard - RĘCZNY przycisk, nigdy automatyczny (patrz routes/pie.py
    - to wywołanie T212 o wąskim rate limicie). ===
*/
async function checkFx() {
    const btn = document.getElementById("btn-fx-check");
    const banner = document.getElementById("fx-guard-banner");
    if (!btn || !banner) return;

    btn.disabled = true;
    banner.classList.add("pie-fx-banner--hidden");

    let total = 0;
    document.querySelectorAll(".pie-asset-row").forEach((row) => {
        total += Number(row.querySelector("[data-amount]").value) || 0;
    });

    try {
        const resp = await fetch(`/pie/${pieId()}/fx-check?total=${total}`);
        const data = await resp.json();

        if (!data.ok) {
            banner.textContent = `Błąd: ${data.error ?? "nieznany"}`;
        } else if (data.warning) {
            banner.textContent = data.warning;
        } else {
            banner.textContent = `Środki wystarczają (dostępne: ${data.free ?? "brak danych"}).`;
        }
        banner.classList.remove("pie-fx-banner--hidden");
    } catch (err) {
        banner.textContent = "Błąd sieci przy sprawdzaniu środków.";
        banner.classList.remove("pie-fx-banner--hidden");
        console.error(err);
    } finally {
        // Cooldown 10s - ten sam powód co przy btn-refresh w warp.js (wąski
        // rate limit T212 na /equity/account/summary).
        setTimeout(() => { btn.disabled = false; }, 10000);
    }
}

/*
=== Dodawanie aktywa - reużywa GET /settings/watchlist/search (lokalny cache
    instrumentów, zero zapytań do T212), tylko POST-uje pod inny endpoint.
    Zakładki kategorii (renderCategoryTabs/fetchCategoryCounts) współdzielone
    z watchlist.js, patrz common.js. ===
*/
let assetSearchTimeout = null;
let assetActiveCategory = "stock";
let assetCurrentOffset = 0;
let assetCachedCounts = null;
const ASSET_PAGE_SIZE = 20;

function renderAssetResults(results, append) {
    const container = document.getElementById("pie-asset-results");
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
        addBtn.addEventListener("click", () => addAssetToPie(r.ticker));
        row.appendChild(addBtn);
        container.appendChild(row);
    });
}

async function loadAssetResults(append) {
    const query = document.getElementById("pie-asset-search").value.trim();
    const offset = append ? assetCurrentOffset : 0;
    const loadMoreBtn = document.getElementById("pie-asset-load-more");

    try {
        const resp = await fetch(
            `/settings/watchlist/search?q=${encodeURIComponent(query)}&category=${assetActiveCategory}&offset=${offset}`
        );
        const data = await resp.json();
        const results = data.results || [];

        renderAssetResults(results, append);
        assetCurrentOffset = offset + results.length;
        loadMoreBtn.style.display = results.length < ASSET_PAGE_SIZE ? "none" : "";
    } catch (err) {
        console.error("Błąd wyszukiwania:", err);
    }
}

function selectAssetCategory(catId) {
    assetActiveCategory = catId;
    renderCategoryTabs(document.getElementById("pie-asset-tabs"), assetCachedCounts, assetActiveCategory, selectAssetCategory);
    loadAssetResults(false);
}

async function addAssetToPie(ticker) {
    const formData = new URLSearchParams();
    formData.set("ticker", ticker);
    await fetch(`/pie/${pieId()}/assets/add`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formData.toString(),
    });
    window.location.reload();
}

/*
=== Wiring ===
*/
document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".pie-asset-row").forEach((row) => {
        const assetId = row.dataset.assetId;

        const slider = row.querySelector("[data-weight-slider]");
        slider.addEventListener("input", () => onSliderInput(row));
        slider.addEventListener("change", () => saveWeight(row));

        const amountInput = row.querySelector("[data-amount]");
        amountInput.addEventListener("input", () => onAmountInput(row));
        amountInput.addEventListener("change", () => saveWeight(row));

        const qtyInput = row.querySelector("[data-qty]");
        qtyInput.addEventListener("input", () => onQuantityInput(row));
        qtyInput.addEventListener("change", () => saveWeight(row));

        row.querySelector("[data-buy]").addEventListener("click", () => buyAsset(row));
    });

    const budgetInput = document.getElementById("pie-budget");
    if (budgetInput) budgetInput.addEventListener("input", onBudgetInput);

    const buyAllBtn = document.getElementById("btn-buy-all");
    if (buyAllBtn) {
        buyAllBtn.addEventListener("click", async () => {
            buyAllBtn.disabled = true;
            const rows = Array.from(document.querySelectorAll(".pie-asset-row"))
                .filter((row) => Number(row.querySelector("[data-qty]").value) > 0);
            for (const row of rows) {
                await buyAsset(row); // sekwencyjnie - cooldown/Hard Cap są per-zlecenie
            }
            buyAllBtn.disabled = false;
        });
    }

    const fxBtn = document.getElementById("btn-fx-check");
    if (fxBtn) fxBtn.addEventListener("click", checkFx);

    const deleteBtn = document.getElementById("btn-delete-pie");
    if (deleteBtn) {
        deleteBtn.addEventListener("click", async () => {
            const confirmed = await confirmDialog(
                "Na pewno usunąć ten koszyk? Aktywa w nim znikną (historia zleceń zostaje)."
            );
            if (confirmed) document.getElementById("delete-pie-form").submit();
        });
    }

    const searchInput = document.getElementById("pie-asset-search");
    if (searchInput) {
        searchInput.addEventListener("input", () => {
            clearTimeout(assetSearchTimeout);
            assetSearchTimeout = setTimeout(() => loadAssetResults(false), 300);
        });
    }

    const assetLoadMoreBtn = document.getElementById("pie-asset-load-more");
    if (assetLoadMoreBtn) {
        assetLoadMoreBtn.addEventListener("click", () => loadAssetResults(true));
    }

    (async () => {
        assetCachedCounts = await fetchCategoryCounts();
        renderCategoryTabs(document.getElementById("pie-asset-tabs"), assetCachedCounts, assetActiveCategory, selectAssetCategory);
        loadAssetResults(false);
    })();

    refreshAllPct();
    loadCharts();
    loadLimits();
});
