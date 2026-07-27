/*
instrument.js
==============
Strona szczegolow pojedynczego instrumentu (/instrument/<ticker>) - cena
live, wykres swiecowy z przelacznikiem zakresu, Kup/Sprzedaj, "Twoja
inwestycja". Wzorowane na focus.js (jeden kafelek zamiast karuzeli) +
pie.js (prog potwierdzenia 70% Hard Cap przed duzym zleceniem).
*/

const ticker = INSTRUMENT_TICKER;
let currentDays = 30;
const DEFAULT_CHART_EMPTY_TEXT = document.getElementById("instrument-chart-empty").textContent.trim();

// --- Wykres swiecowy (TradingView Lightweight Charts, vendorowana lokalnie
// w static/js/vendor/ - self-hosted jak reszta appki, bez CDN) - zastapilo
// reczny SVG renderer 2026-07-27 na prosbe Adama: swiece 1-min (nawet 700+
// na raz) w starym renderze (stale 600px szerokosci, kazda swieca <1px) byly
// widoczne jako "prawie plaska kreska". Biblioteka daje darmowy zoom (kolko
// myszy) i pan (przeciaganie) w poziomie, os Y sama dopasowuje sie do
// aktualnie widocznego zakresu czasu, wiec przybilzenie realnie cos pokazuje.

let priceChart = null;
let candleSeries = null;
let heldAveragePrice = null;
let heldPpl = null;
let tradeLevels = []; // z /warp/trade_levels - [{type: "stop_loss"|"take_profit", price, source}]
let overlayPriceLines = []; // wszystkie linie aktualnie narysowane na candleSeries (srednia + SL/TP)

function themeColor(varName) {
    return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

function addOverlayLine(price, color, style, width, title) {
    if (!candleSeries || price == null) return;
    overlayPriceLines.push(candleSeries.createPriceLine({
        price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title,
    }));
}

// Linie na wykresie: srednia ceny zakupu + stop-loss/take-profit bota, jesli
// pozycja jest przez ktoregos bota zarzadzana (dodane 2026-07-28, Adam:
// "pokazuj linie po jakiej zakupione mam aktywa" + "pokazuj tez linie stop
// loss oraz tp o ile sa, kolory?? hgw sam cos wymysl"):
//   - Twoja srednia: zielona (w zysku) / czerwona (strata) wg ppl, przerywana,
//     grubsza (2px) - ten sam znak co "Twoja inwestycja" (focus-tile__pnl),
//     zeby linia i liczba obok byly spojne.
//   - Stop-loss: czerwona, kropkowana, cienka - kolor "niebezpieczenstwa"
//     spojny ze --sell-red uzywanym wszedzie indziej w appce.
//   - Take-profit: fioletowa (--accent-amber, kolor sygnalu appki) kropkowana,
//     cienka - swiadomie NIE zielona, zeby nie mylila sie z "Twoja srednia"
//     gdy pozycja jest akurat w zysku (dwie zielone linie na raz bylyby
//     nieczytelne).
// Jeden ticker moze teoretycznie miec SL/TP z wiecej niz jednego silnika
// (Micro-Grid/Sygnal/EOD dzialaja niezaleznie) - kazdy dostaje wlasna linie
// z etykieta zrodla w tytule.
function updateOverlayLines() {
    if (!candleSeries) return;
    overlayPriceLines.forEach((line) => candleSeries.removePriceLine(line));
    overlayPriceLines = [];

    if (heldAveragePrice) {
        const color = heldPpl >= 0 ? themeColor("--buy-green") : themeColor("--sell-red");
        addOverlayLine(heldAveragePrice, color, LightweightCharts.LineStyle.Dashed, 2, "Twoja średnia");
    }
    tradeLevels.forEach((lvl) => {
        if (lvl.type === "stop_loss") {
            addOverlayLine(lvl.price, themeColor("--sell-red"), LightweightCharts.LineStyle.Dotted, 1, `Stop-loss (${lvl.source})`);
        } else if (lvl.type === "take_profit") {
            addOverlayLine(lvl.price, themeColor("--accent-amber"), LightweightCharts.LineStyle.Dotted, 1, `Take-profit (${lvl.source})`);
        }
    });
}

async function loadTradeLevels() {
    try {
        const resp = await fetch(`/warp/trade_levels?ticker=${encodeURIComponent(ticker)}`);
        const data = await resp.json();
        if (!data.ok) return;
        tradeLevels = data.levels || [];
        updateOverlayLines();
    } catch (err) {
        console.error("Błąd poziomów SL/TP:", err);
    }
}

function ensureChart() {
    if (priceChart) return;
    const container = document.getElementById("instrument-chart");
    priceChart = LightweightCharts.createChart(container, {
        layout: { background: { color: "transparent" }, textColor: themeColor("--text-muted") },
        grid: {
            vertLines: { color: themeColor("--hairline") },
            horzLines: { color: themeColor("--hairline") },
        },
        rightPriceScale: { borderColor: themeColor("--hairline") },
        timeScale: { borderColor: themeColor("--hairline"), timeVisible: true, secondsVisible: false },
        autoSize: true,
    });
    candleSeries = priceChart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: themeColor("--buy-green"),
        downColor: themeColor("--sell-red"),
        borderVisible: false,
        wickUpColor: themeColor("--buy-green"),
        wickDownColor: themeColor("--sell-red"),
    });
}

async function loadCandles(days, interval) {
    const emptyEl = document.getElementById("instrument-chart-empty");
    emptyEl.style.display = "none";

    try {
        const params = new URLSearchParams({ ticker });
        if (interval) {
            params.set("interval", interval);
        } else {
            params.set("days", days);
        }
        const resp = await fetch(`/warp/candles?${params.toString()}`);
        const data = await resp.json();
        if (!data.ok || !data.candles || data.candles.length < 2) {
            emptyEl.textContent = (!data.ok && data.error) ? data.error : DEFAULT_CHART_EMPTY_TEXT;
            emptyEl.style.display = "";
            return;
        }

        ensureChart();
        // Sort+dedupe po czasie - biblioteka wymaga scisle rosnacej
        // kolejnosci, a Yahoo/Finnhub sporadycznie potrafia dac
        // duplikat/glitch na brzegu zakresu (stary renderer SVG,
        // pozycyjny/bez czasu, na to nie zwracal uwagi).
        const seen = new Set();
        const points = data.candles
            .filter((c) => {
                if (c.t == null || seen.has(c.t)) return false;
                seen.add(c.t);
                return true;
            })
            .sort((a, b) => a.t - b.t)
            .map((c) => ({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c }));

        candleSeries.setData(points);
        priceChart.timeScale().fitContent();
        updateOverlayLines();
    } catch (err) {
        console.error("Błąd wykresu:", err);
        emptyEl.style.display = "";
    }
}

// Zapamietanie wybranej zakladki wykresu w localStorage (dodane 2026-07-28,
// Adam: "przy wykresach dodaj ta sama funkcje co przy botach przy
// odswiezaniu pozycji ma byc w tym samym stanie" - ten sam wzorzec co
// portfolio.js::SORT_STORAGE_KEY i common.js::initReorderablePanels,
// GLOBALNE (nie per-ticker) - jeden wspolny "ostatnio wybrany zakres",
// stosowany przy wejsciu na KAZDY instrument, nie tylko ten sam co przedtem.
const CHART_RANGE_STORAGE_KEY = "snajper-instrument-chart-range";

function saveChartRange(days, interval) {
    try {
        localStorage.setItem(CHART_RANGE_STORAGE_KEY, JSON.stringify({ days, interval }));
    } catch (err) { /* localStorage niedostepny (np. tryb prywatny) - cichy no-op */ }
}

function loadSavedChartRange() {
    try {
        const raw = localStorage.getItem(CHART_RANGE_STORAGE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch (err) {
        return null;
    }
}

// Wolane RAZ przy wejsciu na strone zamiast zawsze-domyslnego "1M" (30 dni) -
// odtwarza ostatnio wybrana zakladke z localStorage. Jesli zapisana zakladka
// to np. "1min", a AKTUALNY ticker nie jest USA (wiec ta zakladka w ogole
// nie istnieje na tej stronie - patrz {% if ticker.endswith('_US_EQ') %} w
// szablonie) - cichy fallback na domyslnie aktywna zakladke w HTML (1M),
// zamiast bledu/pustego wykresu.
function applyInitialChartRange() {
    const tabsContainer = document.getElementById("instrument-range-tabs");
    const saved = loadSavedChartRange();
    let targetBtn = null;
    if (saved) {
        targetBtn = saved.interval
            ? tabsContainer.querySelector(`.tab[data-interval="${saved.interval}"]`)
            : tabsContainer.querySelector(`.tab[data-days="${saved.days}"]`);
    }
    if (!targetBtn) {
        targetBtn = tabsContainer.querySelector(".tab--active") || tabsContainer.querySelector(".tab");
    }

    tabsContainer.querySelectorAll(".tab").forEach((t) => t.classList.remove("tab--active"));
    targetBtn.classList.add("tab--active");

    const interval = targetBtn.dataset.interval || "";
    if (!interval) {
        currentDays = Number(targetBtn.dataset.days);
    }
    loadCandles(currentDays, interval);
}

document.getElementById("instrument-range-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;

    document.querySelectorAll("#instrument-range-tabs .tab").forEach((t) => t.classList.remove("tab--active"));
    btn.classList.add("tab--active");

    // Zakladka "1min" (data-interval) - osobna od data-days (patrz
    // routes/scalping.py::candles ?interval=1m, tylko tickery *_US_EQ).
    const interval = btn.dataset.interval || "";
    if (!interval) {
        currentDays = Number(btn.dataset.days);
    }
    saveChartRange(currentDays, interval);
    loadCandles(currentDays, interval);
});

// --- Cena live ---

function formatChange(dp) {
    if (dp === undefined || dp === null) return "";
    const sign = dp >= 0 ? "+" : "";
    return `${sign}${dp.toFixed(2)}%`;
}

let lastQuote = null;
let week52Range = null;

async function refreshQuote() {
    try {
        const resp = await fetch(`/warp/quote?ticker=${encodeURIComponent(ticker)}`);
        const data = await resp.json();
        if (!data.ok) return;

        lastQuote = data.quote;
        document.getElementById("instrument-price").textContent = lastQuote.c ? lastQuote.c.toFixed(2) : "—";
        const changeEl = document.getElementById("instrument-change");
        changeEl.textContent = formatChange(lastQuote.dp);
        changeEl.className = "instrument-detail__change " +
            (lastQuote.dp >= 0 ? "focus-tile__change--up" : "focus-tile__change--down");

        updateRangeMarkers();
        updateMaxHints();
    } catch (err) {
        console.error("Błąd ceny:", err);
    }
}

/*
=== Statystyki (jak "Statystyki" w apce T212) - Finnhub /stock/metric +
    /stock/profile2 (patrz finnhub_client.py::get_basic_financials/
    get_profile), cache 24h po stronie serwera - jednorazowo przy wejsciu.
    Zakres 1 DZIEN uzywa h/l juz obecnych w /warp/quote (refreshQuote), wiec
    marker aktualizuje sie razem z zywa cena bez dodatkowego requestu.
===*/

function setRangeMarker(markerId, low, high, current) {
    const marker = document.getElementById(markerId);
    if (!marker) return;
    if (low == null || high == null || current == null || high <= low) {
        marker.style.left = "0%";
        return;
    }
    const pct = Math.min(100, Math.max(0, ((current - low) / (high - low)) * 100));
    marker.style.left = `${pct}%`;
}

function updateRangeMarkers() {
    if (!lastQuote) return;
    setRangeMarker("stats-day-marker", lastQuote.l, lastQuote.h, lastQuote.c);
    document.getElementById("stats-day-low").textContent = lastQuote.l != null ? lastQuote.l.toFixed(2) : "—";
    document.getElementById("stats-day-high").textContent = lastQuote.h != null ? lastQuote.h.toFixed(2) : "—";
    if (week52Range) {
        setRangeMarker("stats-52w-marker", week52Range.low, week52Range.high, lastQuote.c);
    }
}

function formatMarketCap(millions) {
    if (millions == null) return "—";
    if (millions >= 1e6) return `${(millions / 1e6).toFixed(2)} bln`;
    if (millions >= 1e3) return `${(millions / 1e3).toFixed(2)} mld`;
    return `${millions.toFixed(1)} mln`;
}

function formatVolume(millions) {
    if (millions == null) return "—";
    return `${millions.toFixed(1)} mln`;
}

async function loadStats() {
    try {
        const resp = await fetch(`/warp/stats?ticker=${encodeURIComponent(ticker)}`);
        const data = await resp.json();
        if (!data.ok) return;

        document.getElementById("stats-52w-low").textContent = data.week52_low != null ? data.week52_low.toFixed(2) : "—";
        document.getElementById("stats-52w-high").textContent = data.week52_high != null ? data.week52_high.toFixed(2) : "—";
        document.getElementById("stats-market-cap").textContent = formatMarketCap(data.market_cap);
        document.getElementById("stats-avg-volume").textContent = formatVolume(data.avg_volume_3m);
        document.getElementById("stats-pe").textContent = data.pe_ttm != null ? data.pe_ttm.toFixed(2) : "—";
        document.getElementById("stats-dividend").textContent = data.dividend_yield != null ? `${data.dividend_yield.toFixed(2)}%` : "—";

        if (data.week52_low != null && data.week52_high != null) {
            week52Range = { low: data.week52_low, high: data.week52_high };
        }

        document.getElementById("instrument-stats").style.display = "";
        updateRangeMarkers();
    } catch (err) {
        console.error("Błąd statystyk:", err);
    }
}

/*
=== "Twoja inwestycja" - JEDNORAZOWO przy wejsciu na strone (nie na
    interwale), ten sam powod co loadFocusPnl w focus.js: waski rate limit
    T212 na /equity/portfolio. Reuzywa /warp/account, ktory juz zwraca
    wszystkie pozycje - filtrujemy po stronie klienta do jednego tickera. ===
*/
async function loadPosition() {
    try {
        const resp = await fetch("/warp/account");
        const data = await resp.json();
        if (!data.ok) {
            // Najczesciej waski rate limit T212 na demo - bez tego komunikatu
            // "Max kupno/sprzedaz" po prostu zostawaly na "-" bez wyjasnienia.
            const buyEl = document.getElementById("max-buy-hint");
            const sellEl = document.getElementById("max-sell-hint");
            if (buyEl) buyEl.textContent = `Max kupno: błąd (${data.error || "T212"})`;
            if (sellEl) sellEl.textContent = "Max sprzedaż: błąd";
            return;
        }

        heldQuantity = 0;
        availableCash = data.cash && data.cash.free != null ? Number(data.cash.free) : null;

        const position = (data.positions || []).find((p) => p.ticker === ticker);
        if (position) {
            const qty = Number(position.quantity) || 0;
            const currentPrice = Number(position.currentPrice) || 0;
            const avgPrice = Number(position.averagePrice) || 0;
            const ppl = Number(position.ppl) || 0;

            heldQuantity = qty;

            document.getElementById("position-value").textContent = (qty * currentPrice).toFixed(2);
            const pplEl = document.getElementById("position-pnl");
            pplEl.textContent = `${ppl >= 0 ? "+" : ""}${ppl.toFixed(2)}`;
            pplEl.className = "instrument-detail__position-value " + (ppl >= 0 ? "focus-tile__pnl--profit" : "focus-tile__pnl--loss");
            document.getElementById("position-qty").textContent = qty;
            document.getElementById("position-avg").textContent = avgPrice.toFixed(2);

            document.getElementById("instrument-position").style.display = "";

            heldAveragePrice = avgPrice > 0 ? avgPrice : null;
            heldPpl = ppl;
            updateOverlayLines();
        }

        updateMaxHints();
    } catch (err) {
        console.error("Błąd pozycji:", err);
        const buyEl = document.getElementById("max-buy-hint");
        const sellEl = document.getElementById("max-sell-hint");
        if (buyEl) buyEl.textContent = "Max kupno: błąd sieci";
        if (sellEl) sellEl.textContent = "Max sprzedaż: błąd sieci";
    }
}

// --- Ilość (presety + własna) - ten sam wzorzec co focus.js/warp.js ---

function setupPresets() {
    const presets = document.querySelectorAll(".tile__preset");
    const customInput = document.getElementById("instrument-qty-custom");
    presets.forEach((btn) => {
        btn.addEventListener("click", () => {
            presets.forEach((b) => b.classList.remove("tile__preset--active"));
            btn.classList.add("tile__preset--active");
            if (btn.dataset.qty === "custom") {
                customInput.classList.remove("tile__qty-custom-input--hidden");
                customInput.focus();
            } else {
                customInput.classList.add("tile__qty-custom-input--hidden");
            }
        });
    });
}

/*
=== MAX - inne niz stale presety (0.1/0.5/1) bo "maksimum" zalezy od kierunku
    zlecenia (max KUPNA = dostepna gotowka / cena, max SPRZEDAZY = ile masz
    tego aktywa) - nie da sie tego zamienic na jedna z gory liczbe jak reszta
    presetow. Dlatego getQuantity() przyjmuje `side` i liczy dopiero w
    momencie wysylki, a maxBuyQuantity()/maxSellQuantity() sa tez uzywane
    do na biezaco wyswietlanych podpowiedzi pod przyciskami (updateMaxHints).
===*/
let availableCash = null;
let heldQuantity = null;

function maxBuyQuantity() {
    const price = lastQuote && lastQuote.c ? Number(lastQuote.c) : null;
    if (availableCash == null || !price) return null;
    let qty = availableCash / price;
    if (cachedMaxOrderValue) qty = Math.min(qty, cachedMaxOrderValue / price);
    return Math.max(0, qty);
}

function updateMaxHints() {
    const buyEl = document.getElementById("max-buy-hint");
    const sellEl = document.getElementById("max-sell-hint");
    if (!buyEl || !sellEl) return;

    const maxBuy = maxBuyQuantity();
    buyEl.textContent = `Max kupno: ${maxBuy != null ? maxBuy.toFixed(4) : "—"} szt.`;
    sellEl.textContent = `Max sprzedaż: ${heldQuantity != null ? heldQuantity : "—"} szt.`;
}

function getQuantity(side) {
    const active = document.querySelector(".tile__preset--active");
    if (active && active.dataset.qty === "custom") {
        return document.getElementById("instrument-qty-custom").value;
    }
    if (active && active.dataset.qty === "max") {
        if (side === "sell") return heldQuantity != null ? String(heldQuantity) : "0";
        const maxBuy = maxBuyQuantity();
        return maxBuy != null ? maxBuy.toFixed(4) : "0";
    }
    if (active) return active.dataset.qty;
    return document.getElementById("instrument-qty-custom").value;
}

// --- Zlecenie - prog potwierdzenia 70% Hard Cap, ten sam co pie.js/warp.js ---

let cachedMaxOrderValue = null;
const CONFIRM_THRESHOLD_RATIO = 0.7;

async function loadLimits() {
    try {
        const resp = await fetch("/warp/limits");
        const data = await resp.json();
        cachedMaxOrderValue = data.max_order_value ? Number(data.max_order_value) : null;
        updateMaxHints();
    } catch (err) {
        console.error("Błąd limitów:", err);
    }
}

async function sendOrder(side) {
    const quantity = getQuantity(side);
    const statusEl = document.getElementById("instrument-status");
    const btns = document.querySelectorAll(".focus-tile__btn");

    if (!quantity || Number(quantity) <= 0) {
        const active = document.querySelector(".tile__preset--active");
        if (active && active.dataset.qty === "max") {
            // MAX policzyl 0 - wyjasnij dlaczego zamiast ogolnego "podaj ilosc"
            // (myli, skoro user NIC nie musial wpisywac recznie).
            statusEl.textContent = side === "sell"
                ? "Nie masz nic do sprzedania (posiadasz 0 szt.)."
                : "Brak środków na zakup (albo nie udało się pobrać salda/ceny).";
        } else {
            statusEl.textContent = "Podaj ilość > 0";
        }
        return;
    }

    const price = lastQuote && lastQuote.c ? Number(lastQuote.c) : null;
    if (price && cachedMaxOrderValue) {
        const estValue = Number(quantity) * price;
        if (estValue >= cachedMaxOrderValue * CONFIRM_THRESHOLD_RATIO) {
            const confirmed = await confirmDialog(
                `Duże zlecenie: ${side === "buy" ? "KUP" : "SPRZEDAJ"} ${quantity} × ${ticker.split("_")[0]} ` +
                `≈ ${estValue.toFixed(2)} (limit: ${cachedMaxOrderValue.toFixed(2)}). Kontynuować?`
            );
            if (!confirmed) return;
        }
    }

    btns.forEach((b) => (b.disabled = true));
    statusEl.textContent = "wysyłanie…";

    try {
        const resp = await fetch("/warp/order", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker, side, quantity, estimated_price: price }),
        });
        const data = await resp.json();

        if (data.ok) {
            playSuccess();
            statusEl.textContent = `OK #${data.order_id ?? "?"}`;
            const flash = document.getElementById("instrument-flash");
            flash.classList.add("focus-tile__flash--ok");
            setTimeout(() => flash.classList.remove("focus-tile__flash--ok"), 400);
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
        btns.forEach((b) => (b.disabled = false));
    }
}

document.querySelector(".instrument-detail__actions").addEventListener("click", (e) => {
    const btn = e.target.closest(".focus-tile__btn");
    if (btn) sendOrder(btn.dataset.side);
});

document.getElementById("btn-instrument-back").addEventListener("click", () => {
    if (document.referrer) {
        history.back();
    } else {
        window.location.href = "/warp/";
    }
});

// Klik na liczbę "Akcje" w widgecie "Twoja inwestycja" = skrót do MAX w ILOŚĆ
// (ten sam efekt co ręczne kliknięcie przycisku MAX niżej) - potem wystarczy
// od razu kliknąć SPRZEDAJ, bez szukania przycisku w innym miejscu ekranu.
document.getElementById("position-qty").addEventListener("click", () => {
    const maxBtn = document.querySelector('.tile__preset[data-qty="max"]');
    if (maxBtn) maxBtn.click();
});

// --- Init ---

setupPresets();
applyInitialChartRange();
refreshQuote();
loadPosition();
loadTradeLevels();
loadLimits();
loadStats();
setInterval(refreshQuote, 5000);
