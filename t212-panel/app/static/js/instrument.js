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
    } catch (err) {
        console.error("Błąd ceny:", err);
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

        const position = (data.positions || []).find((p) => p.ticker === ticker);
        if (!position) return;

        const qty = Number(position.quantity) || 0;
        const currentPrice = Number(position.currentPrice) || 0;
        const avgPrice = Number(position.averagePrice) || 0;
        const ppl = Number(position.ppl) || 0;

        document.getElementById("position-value").textContent = (qty * currentPrice).toFixed(2);
        const pplEl = document.getElementById("position-pnl");
        pplEl.textContent = `${ppl >= 0 ? "+" : ""}${ppl.toFixed(2)}`;
        pplEl.className = "instrument-detail__position-value " + (ppl >= 0 ? "focus-tile__pnl--profit" : "focus-tile__pnl--loss");
        document.getElementById("position-qty").textContent = qty;
        document.getElementById("position-avg").textContent = avgPrice.toFixed(2);

        document.getElementById("instrument-position").style.display = "";
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

function getQuantity() {
    const active = document.querySelector(".tile__preset--active");
    if (active && active.dataset.qty !== "custom") return active.dataset.qty;
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
    } catch (err) {
        console.error("Błąd limitów:", err);
    }
}

async function sendOrder(side) {
    const quantity = getQuantity();
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
setInterval(refreshQuote, 5000);
