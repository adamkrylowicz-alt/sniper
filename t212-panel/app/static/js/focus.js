/*
focus.js
=========
Focus Mode: karuzela kafelkow, cena live z Finnhub, sparkline SVG.

Nawigacja: strzalki klawiatury <- -> + przyciski w UI.
Aktywne kafelki: FOCUS_TILES (z szablonu) - te ktore sa widoczne.
Odswiezanie: REFRESH_INTERVAL_MS (dynamicznie obliczone przez backend).
*/

let currentIndex = 0;

// --- Karuzela ---

function getVisibleTickers() {
    const tiles = document.querySelectorAll(".focus-tile");
    const visible = [];
    for (let i = 0; i < FOCUS_TILES; i++) {
        const idx = (currentIndex + i) % TOTAL_TICKERS;
        visible.push(tiles[idx]);
    }
    return visible;
}

function updateCarousel() {
    const tiles = document.querySelectorAll(".focus-tile");
    const dots = document.querySelectorAll(".focus-dot");

    tiles.forEach((tile, i) => {
        const offset = ((i - currentIndex) % TOTAL_TICKERS + TOTAL_TICKERS) % TOTAL_TICKERS;
        if (offset < FOCUS_TILES) {
            tile.classList.add("focus-tile--visible");
            tile.classList.remove("focus-tile--hidden");
        } else {
            tile.classList.remove("focus-tile--visible");
            tile.classList.add("focus-tile--hidden");
        }
    });

    dots.forEach((dot, i) => {
        dot.classList.toggle("focus-dot--active", i === currentIndex);
    });
}

function navigate(dir) {
    currentIndex = ((currentIndex + dir) % TOTAL_TICKERS + TOTAL_TICKERS) % TOTAL_TICKERS;
    updateCarousel();
}

document.getElementById("focus-prev").addEventListener("click", () => navigate(-1));
document.getElementById("focus-next").addEventListener("click", () => navigate(1));

document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") navigate(-1);
    if (e.key === "ArrowRight") navigate(1);
});

document.querySelectorAll(".focus-dot").forEach((dot, i) => {
    dot.addEventListener("click", () => {
        currentIndex = i;
        updateCarousel();
    });
});

// --- Swiece OHLC (SVG) ---
// Kolory przez klasy CSS (.focus-candle-up/--down, patrz style.css) zamiast
// twardo wpisanych hexow - podazaja za aktualnym motywem (Signal Room itp.)
// zamiast zamrozonych na starej palecie.

function drawCandles(svgEl, candles) {
    if (!candles || candles.length < 2) return;

    const W = 200, H = 60, PAD = 4;
    const min = Math.min(...candles.map((c) => c.l));
    const max = Math.max(...candles.map((c) => c.h));
    const range = (max - min) || 1;
    const y = (v) => H - PAD - ((v - min) / range) * (H - PAD * 2);

    const n = candles.length;
    const slot = (W - PAD * 2) / n;
    const bodyWidth = Math.max(1.2, slot * 0.6);

    const parts = candles.map((c, i) => {
        const x = PAD + slot * i + slot / 2;
        const cls = c.c >= c.o ? "focus-candle-up" : "focus-candle-down";
        const yOpen = y(c.o), yClose = y(c.c);
        const bodyTop = Math.min(yOpen, yClose);
        const bodyH = Math.max(0.8, Math.abs(yClose - yOpen));
        return (
            `<line class="${cls}" x1="${x.toFixed(1)}" y1="${y(c.h).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(c.l).toFixed(1)}" stroke-width="1"/>` +
            `<rect class="${cls}" x="${(x - bodyWidth / 2).toFixed(1)}" y="${bodyTop.toFixed(1)}" width="${bodyWidth.toFixed(1)}" height="${bodyH.toFixed(1)}"/>`
        );
    }).join("");

    svgEl.innerHTML = parts;
}

// --- Cena live ---

function formatPrice(c) {
    if (!c || c === 0) return "—";
    return c.toFixed(2);
}

function formatChange(dp) {
    if (dp === undefined || dp === null) return "";
    const sign = dp >= 0 ? "+" : "";
    return `${sign}${dp.toFixed(2)}%`;
}

async function refreshQuotes() {
    const tiles = document.querySelectorAll(".focus-tile");
    // Odpytujemy tylko widoczne kafelki - oszczedzamy limit Finnhub
    for (let i = 0; i < FOCUS_TILES; i++) {
        const idx = (currentIndex + i) % TOTAL_TICKERS;
        const tile = tiles[idx];
        if (!tile) continue;

        const ticker = tile.dataset.ticker;
        try {
            const resp = await fetch(`/warp/quote?ticker=${encodeURIComponent(ticker)}`);
            const data = await resp.json();
            if (!data.ok) continue;

            const q = data.quote;
            const priceEl = document.getElementById(`price-${ticker}`);
            const changeEl = document.getElementById(`change-${ticker}`);
            if (priceEl) priceEl.textContent = formatPrice(q.c);
            if (changeEl) {
                changeEl.textContent = formatChange(q.dp);
                changeEl.className = "focus-tile__change " +
                    (q.dp >= 0 ? "focus-tile__change--up" : "focus-tile__change--down");
            }
        } catch (err) {
            console.error("Quote error:", ticker, err);
        }
    }
}

async function loadCandles(ticker) {
    try {
        const resp = await fetch(`/warp/candles?ticker=${encodeURIComponent(ticker)}`);
        const data = await resp.json();
        const svgEl = document.getElementById(`spark-${ticker}`);
        if (!svgEl) return;
        if (!data.ok) {
            svgEl.innerHTML = '<text x="100" y="35" text-anchor="middle" fill="#7C868C" font-size="10" font-family="monospace">brak danych</text>';
            return;
        }
        drawCandles(svgEl, data.candles);
    } catch (err) {
        console.error("Candles error:", ticker, err);
    }
}

/*
Zysk/strata z faktycznej pozycji (jesli posiadasz dany ticker) - JEDNORAZOWO
przy wejsciu na strone, NIE na interwale (T212 ma bardzo waski rate limit na
/equity/portfolio, patrz warp.js::MIN_LOAD_GAP_MS - Focus Mode nie powinien
dokladac wlasnych, niezaleznych requestow do tego samego, juz napietego
budzetu). Dla tickerow bez pozycji zostaje puste, bez zgadywania.
*/
async function loadFocusPnl() {
    try {
        const resp = await fetch("/warp/account");
        const data = await resp.json();
        if (!data.ok) return;

        const positionsByTicker = {};
        (data.positions || []).forEach((p) => { positionsByTicker[p.ticker] = p; });

        document.querySelectorAll(".focus-tile").forEach((tile) => {
            const pnlEl = document.getElementById(`pnl-${tile.dataset.ticker}`);
            const position = positionsByTicker[tile.dataset.ticker];
            if (!pnlEl || !position) return;

            const ppl = Number(position.ppl);
            const sign = ppl >= 0 ? "+" : "";
            pnlEl.textContent = `${position.quantity} szt. · ${sign}${ppl.toFixed(2)}`;
            pnlEl.className = "focus-tile__pnl " + (ppl >= 0 ? "focus-tile__pnl--profit" : "focus-tile__pnl--loss");
        });
    } catch (err) {
        console.error("Focus P&L error:", err);
    }
}

// --- Zlecenia (analogicznie do warp.js) ---

function setupFocusPresets(tile) {
    const presets = tile.querySelectorAll(".tile__preset");
    const customInput = tile.querySelector(".tile__qty-custom-input");
    presets.forEach(btn => {
        btn.addEventListener("click", () => {
            presets.forEach(b => b.classList.remove("tile__preset--active"));
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

function getFocusQuantity(tile) {
    const active = tile.querySelector(".tile__preset--active");
    if (active && active.dataset.qty !== "custom") return active.dataset.qty;
    return tile.querySelector(".tile__qty-custom-input").value;
}

async function sendFocusOrder(tile, side) {
    const ticker = tile.dataset.ticker;
    const quantity = getFocusQuantity(tile);
    const statusEl = tile.querySelector(".focus-tile__status");
    const btns = tile.querySelectorAll(".focus-tile__btn");

    if (!quantity || Number(quantity) <= 0) {
        statusEl.textContent = "Podaj ilość > 0";
        return;
    }

    btns.forEach(b => b.disabled = true);
    statusEl.textContent = "wysyłanie…";

    try {
        const resp = await fetch("/warp/order", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker, side, quantity }),
        });
        const data = await resp.json();
        if (data.ok) {
            statusEl.textContent = `OK #${data.order_id ?? "?"}`;
            tile.querySelector(".focus-tile__flash").classList.add("focus-tile__flash--ok");
            setTimeout(() => tile.querySelector(".focus-tile__flash").classList.remove("focus-tile__flash--ok"), 400);
        } else {
            statusEl.textContent = `BŁĄD: ${data.error ?? "nieznany"}`;
        }
    } catch (err) {
        statusEl.textContent = "Błąd sieci";
    } finally {
        btns.forEach(b => b.disabled = false);
    }
}

// --- Init ---

document.querySelectorAll(".focus-tile").forEach(tile => {
    setupFocusPresets(tile);
    tile.querySelector(".focus-tile__actions").addEventListener("click", e => {
        const btn = e.target.closest(".focus-tile__btn");
        if (btn) sendFocusOrder(tile, btn.dataset.side);
    });
});

// Inicjalizacja karuzelki
updateCarousel();

// Ladowanie swiec dla wszystkich przy starcie (dane dzienne, cache 1h) +
// zysk/strata JEDNORAZOWO (patrz docstring loadFocusPnl - rate limit T212).
document.querySelectorAll(".focus-tile").forEach(tile => {
    loadCandles(tile.dataset.ticker);
});
loadFocusPnl();

// Odswiezanie cen live co dynamicznie obliczony interwal
refreshQuotes();
setInterval(refreshQuotes, REFRESH_INTERVAL_MS);
