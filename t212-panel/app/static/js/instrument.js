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

// --- Swiece OHLC (SVG) - ten sam algorytm co focus.js/pie.js, wiekszy viewBox ---

function drawCandles(svgEl, candles) {
    const W = 600, H = 220, PAD = 8;
    const min = Math.min(...candles.map((c) => c.l));
    const max = Math.max(...candles.map((c) => c.h));
    const range = (max - min) || 1;
    const y = (v) => H - PAD - ((v - min) / range) * (H - PAD * 2);

    const n = candles.length;
    const slot = (W - PAD * 2) / n;
    const bodyWidth = Math.max(1, slot * 0.6);

    const parts = candles.map((c, i) => {
        const x = PAD + slot * i + slot / 2;
        const cls = c.c >= c.o ? "focus-candle-up" : "focus-candle-down";
        const yOpen = y(c.o), yClose = y(c.c);
        const bodyTop = Math.min(yOpen, yClose);
        const bodyH = Math.max(1, Math.abs(yClose - yOpen));
        return (
            `<line class="${cls}" x1="${x.toFixed(1)}" y1="${y(c.h).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(c.l).toFixed(1)}" stroke-width="1"/>` +
            `<rect class="${cls}" x="${(x - bodyWidth / 2).toFixed(1)}" y="${bodyTop.toFixed(1)}" width="${bodyWidth.toFixed(1)}" height="${bodyH.toFixed(1)}"/>`
        );
    }).join("");

    svgEl.innerHTML = parts;
}

async function loadCandles(days) {
    const svgEl = document.getElementById("instrument-chart");
    const emptyEl = document.getElementById("instrument-chart-empty");
    svgEl.innerHTML = "";
    emptyEl.style.display = "none";

    try {
        const resp = await fetch(`/warp/candles?ticker=${encodeURIComponent(ticker)}&days=${days}`);
        const data = await resp.json();
        if (!data.ok || !data.candles || data.candles.length < 2) {
            emptyEl.style.display = "";
            return;
        }
        drawCandles(svgEl, data.candles);
    } catch (err) {
        console.error("Błąd wykresu:", err);
        emptyEl.style.display = "";
    }
}

document.getElementById("instrument-range-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;

    document.querySelectorAll("#instrument-range-tabs .tab").forEach((t) => t.classList.remove("tab--active"));
    btn.classList.add("tab--active");
    currentDays = Number(btn.dataset.days);
    loadCandles(currentDays);
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
        if (!data.ok) return;

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
        }

        updateMaxHints();
    } catch (err) {
        console.error("Błąd pozycji:", err);
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
        statusEl.textContent = "Podaj ilość > 0";
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

// --- Init ---

setupPresets();
loadCandles(currentDays);
refreshQuote();
loadPosition();
loadLimits();
loadStats();
setInterval(refreshQuote, 5000);
