/**
 * app/static/js/obligacje.js
 * ==========================
 * Logika dashboarda obligacji: odświeżanie statusu, kursy live, historia.
 */

const REFRESH_INTERVAL_MS = 30000; // 30 sekund
let statusRefreshTimer = null;
let quotesRefreshTimer = null;
let currentSuggestion = null; // {ticker, amount_eur} z ostatniego /status, do przycisku Kup

/**
 * Inicjalizacja przy wejściu na stronę.
 */
document.addEventListener("DOMContentLoaded", () => {
    refreshStatus();
    refreshQuotes();
    loadHistory();
    loadUniverse();

    statusRefreshTimer = setInterval(refreshStatus, REFRESH_INTERVAL_MS);
    quotesRefreshTimer = setInterval(refreshQuotes, REFRESH_INTERVAL_MS);

    document.getElementById("buy-rebalance-btn").addEventListener("click", buyRebalance);
    // Delegacja - jeden listener na całą tabelę zamiast per-wiersz (wiersze
    // są renderowane dynamicznie w renderUniverse()).
    document.getElementById("universe-tbody").addEventListener("click", (e) => {
        if (e.target.matches("button[data-ticker]")) {
            buyUniverseRow(e.target.dataset.ticker);
        }
    });
});

async function buyRebalance() {
    if (!currentSuggestion) return;
    const btn = document.getElementById("buy-rebalance-btn");
    const resultEl = document.getElementById("rebalance-result");
    btn.disabled = true;
    resultEl.textContent = "Wysyłanie zlecenia...";
    resultEl.style.color = "var(--text-muted)";

    try {
        const response = await fetch("/obligacje/kup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker: currentSuggestion.ticker, amount_eur: currentSuggestion.amount_eur }),
        });
        const data = await response.json();
        if (!response.ok) {
            resultEl.textContent = data.error || `Błąd HTTP ${response.status}`;
            resultEl.style.color = "var(--sell-red)";
        } else {
            resultEl.textContent = `Zlecenie wysłane (order_id ${data.order_id}).`;
            resultEl.style.color = "var(--buy-green)";
            refreshStatus();
            loadHistory();
        }
    } catch (e) {
        resultEl.textContent = "Błąd sieci: " + e.message;
        resultEl.style.color = "var(--sell-red)";
    } finally {
        btn.disabled = false;
    }
}

/**
 * Pobiera i renderuje status: allocation, portfel, kupon, performance.
 */
async function refreshStatus() {
    try {
        const response = await fetch("/obligacje/status");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        // Alokacja (gauge)
        renderAllocation(data.allocation);

        // Portfel ETF-ów
        renderPortfolio(data.portfolio, data.allocation);

        // Kupon YTD
        renderCoupon(data.coupon_ytd_eur, data.coupon_ytd_pct);

        // Performance vs SPY
        renderPerformance(data.performance_30d);

        // Sugestia rebalancingu
        renderRebalancingPrompt(data.rebalancing_suggestion);
    } catch (e) {
        console.error("refreshStatus failed:", e);
        applyMoneyHiding();
    }
}

function renderAllocation(alloc) {
    document.getElementById("stocks-pct").textContent = alloc.stocks_pct.toFixed(1) + "%";
    document.getElementById("bonds-pct").textContent = alloc.bonds_pct.toFixed(1) + "%";
}

function renderPortfolio(portfolio, alloc) {
    const tbody = document.getElementById("portfolio-tbody");
    if (!portfolio || portfolio.length === 0) {
        tbody.innerHTML = "<tr><td colspan='7' style='text-align: center; padding: 1rem; color: #999;'>Brak obligacji</td></tr>";
        return;
    }

    const total_value = portfolio.reduce((sum, p) => sum + p.value_eur, 0);

    tbody.innerHTML = portfolio.map(pos => {
        const pct = total_value > 0 ? (pos.value_eur / total_value * 100).toFixed(1) : 0;
        // Kupon roczny = wartość × yield
        const annual_coupon = (pos.value_eur * (pos.ticker === "VGOV" ? 3.8 : pos.ticker === "IERC" ? 4.2 : 6.8) / 100).toFixed(2);

        return `<tr>
            <td>${pos.ticker}</td>
            <td>${pos.quantity.toFixed(4)}</td>
            <td>${pos.avg_price.toFixed(2)}€</td>
            <td>${pos.current_price.toFixed(2)}€</td>
            <td class="js-money">${pos.value_eur.toFixed(2)}€</td>
            <td>${pct}%</td>
            <td class="js-money">${annual_coupon}€</td>
        </tr>`;
    }).join("");

    applyMoneyHiding();
}

function renderCoupon(eur_amount, pct) {
    document.getElementById("coupon-eur").textContent = eur_amount.toFixed(2) + "€";
    document.getElementById("coupon-pct").textContent = pct.toFixed(1) + "%";
    document.getElementById("coupon-bar").style.width = Math.min(100, pct) + "%";
}

function renderPerformance(perf) {
    const bondsReturn = perf.bonds_return_pct || 0;
    const spyReturn = perf.spy_return_pct || 0;

    document.getElementById("bonds-return").textContent = (bondsReturn >= 0 ? "+" : "") + bondsReturn.toFixed(1) + "%";
    document.getElementById("bonds-return").style.color = bondsReturn >= 0 ? "#4caf50" : "#ff6b6b";

    document.getElementById("spy-return").textContent = (spyReturn >= 0 ? "+" : "") + spyReturn.toFixed(1) + "%";
    document.getElementById("spy-return").style.color = spyReturn >= 0 ? "#4caf50" : "#ff6b6b";

    const outperf = bondsReturn - spyReturn;
    document.getElementById("outperformance").textContent = (outperf >= 0 ? "+" : "") + outperf.toFixed(1) + "%";
    document.getElementById("outperformance").style.color = outperf >= 0 ? "#2196F3" : "#ff9800";
}

function renderRebalancingPrompt(suggestion) {
    const section = document.getElementById("rebalance-section");
    if (!suggestion) {
        currentSuggestion = null;
        section.style.display = "none";
        return;
    }

    currentSuggestion = { ticker: suggestion.ticker, amount_eur: suggestion.amount_eur };
    document.getElementById("rebalance-text").textContent = suggestion.text;
    document.getElementById("rebalance-result").textContent = "";
    section.style.display = "block";
}

/**
 * Pobiera i renderuje live kursy: VGOV, IERC, IHYE, SPY.
 */
async function refreshQuotes() {
    try {
        const response = await fetch("/obligacje/kursy");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        renderQuotes(data);
    } catch (e) {
        console.error("refreshQuotes failed:", e);
    }
}

function renderQuotes(quotes) {
    const tbody = document.getElementById("quotes-tbody");
    const tickers = ["SPYLa_EQ"]; // tylko benchmark - reszta funduszy jest w tabeli "universe" niżej

    tbody.innerHTML = tickers.map(ticker => {
        const quote = quotes[ticker];
        if (!quote) {
            return `<tr><td>${ticker}</td><td colspan="2" style="color:var(--text-muted)">Brak danych</td></tr>`;
        }

        const changeText = quote.change_pct != null
            ? (quote.change_pct >= 0 ? "+" : "") + quote.change_pct.toFixed(2) + "%"
            : "—";
        return `<tr>
            <td>${ticker}</td>
            <td class="js-money">${quote.price ? quote.price.toFixed(2) + "€" : "—"}</td>
            <td>${changeText}</td>
        </tr>`;
    }).join("");

    applyMoneyHiding();
}

/**
 * Pobiera historię kupów z OrderLog.
 */
async function loadHistory() {
    try {
        const response = await fetch("/obligacje/historia");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        renderHistory(data);
    } catch (e) {
        console.error("loadHistory failed:", e);
    }
}

function renderHistory(entries) {
    const tbody = document.getElementById("historia-tbody");
    if (!entries || entries.length === 0) {
        tbody.innerHTML = "<tr><td colspan='6' style='text-align: center; padding: 1rem; color: #999;'>Brak historii</td></tr>";
        return;
    }

    tbody.innerHTML = entries.map(e => {
        const date = new Date(e.created_at).toLocaleString("pl-PL");
        const total = (e.quantity * (e.price || 0)).toFixed(2);

        return `<tr>
            <td>${date}</td>
            <td>${e.ticker}</td>
            <td>${e.side === "buy" ? "Kupno" : "Sprzedaż"}</td>
            <td>${e.quantity.toFixed(4)}</td>
            <td class="js-money">${e.price ? e.price.toFixed(2) + "€" : "—"}</td>
            <td class="js-money">${total}€</td>
        </tr>`;
    }).join("");

    applyMoneyHiding();
}

/**
 * Pobiera pełną listę śledzonych obligacyjnych ETF-ów (BOND_UNIVERSE) z
 * żywą ceną - do ręcznego przeglądania/kupowania dowolnego funduszu.
 */
async function loadUniverse() {
    try {
        const response = await fetch("/obligacje/universe");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        renderUniverse(data);
    } catch (e) {
        console.error("loadUniverse failed:", e);
    }
}

function renderUniverse(list) {
    const tbody = document.getElementById("universe-tbody");
    if (!list || list.length === 0) {
        tbody.innerHTML = "<tr><td colspan='6' style='text-align:center; padding:16px; color:var(--text-muted)'>Brak funduszy</td></tr>";
        return;
    }

    tbody.innerHTML = list.map(b => {
        const priceText = b.price != null ? b.price.toFixed(2) + "€" : "—";
        const changeText = b.change_pct != null
            ? (b.change_pct >= 0 ? "+" : "") + b.change_pct.toFixed(2) + "%"
            : "—";
        const descAttr = escapeHtmlAttr(b.description || "");
        return `<tr>
            <td>${b.category}</td>
            <td>
                ${b.name}
                <span class="icon-info" tabindex="0" title="${descAttr}">
                    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><path d="M12 16v-5M12 8h.01"/></svg>
                </span>
                <br><span style="color:var(--text-muted); font-size:11px">${b.ticker}</span>
            </td>
            <td class="js-money">${priceText}</td>
            <td>${changeText}</td>
            <td><input type="number" class="auth-form__input" data-amount-for="${b.ticker}" placeholder="np. 100" min="0" step="0.01" style="width:100px"></td>
            <td><button class="auth-form__submit" data-ticker="${b.ticker}" style="width:auto">Kup</button></td>
        </tr>`;
    }).join("");

    applyMoneyHiding();
}

function escapeHtmlAttr(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML.replace(/"/g, "&quot;");
}

async function buyUniverseRow(ticker) {
    const amountInput = document.querySelector(`input[data-amount-for="${ticker}"]`);
    const resultEl = document.getElementById("universe-result");
    const amount = parseFloat(amountInput.value);

    if (!amount || amount <= 0) {
        resultEl.textContent = "Podaj prawidłową kwotę.";
        resultEl.style.color = "var(--sell-red)";
        return;
    }

    resultEl.textContent = "Wysyłanie zlecenia...";
    resultEl.style.color = "var(--text-muted)";

    try {
        const response = await fetch("/obligacje/kup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker, amount_eur: amount }),
        });
        const data = await response.json();
        if (!response.ok) {
            resultEl.textContent = data.error || `Błąd HTTP ${response.status}`;
            resultEl.style.color = "var(--sell-red)";
        } else {
            resultEl.textContent = `${ticker}: zlecenie wysłane (order_id ${data.order_id}).`;
            resultEl.style.color = "var(--buy-green)";
            refreshStatus();
            loadHistory();
        }
    } catch (e) {
        resultEl.textContent = "Błąd sieci: " + e.message;
        resultEl.style.color = "var(--sell-red)";
    }
}
