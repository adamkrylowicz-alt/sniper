/*
portfolio.js
=============
"Aktywa" - strona laduje sie NATYCHMIAST z cache (patrz scalping.py::
portfolio_view - _portfolio_cache w pamieci procesu), a swiezy odczyt z T212
dociagamy tutaj z opoznieniem (ten sam powod co 1.5s opoznienie "Otwartych
zlecen" w warp.js: nie strzelac zapytaniem do T212 zaraz po zaladowaniu
strony, waski rate limit demo - stad tez czesty blad gdy odpalone za
wczesnie/za czesto). Sukces sygnalizowany OSOBNYM dzwiekiem (playUpdate,
nie playSuccess/playError - to nie jest wynik zlecenia Kup/Sprzedaz, tylko
cichy refresh danych w tle). Blad odswiezenia NIE psuje strony - zostaje to
co bylo w cache, tylko status pod tytulem informuje ze sie nie udalo.
*/

const REFRESH_DELAY_MS = 1500;

// Sortowanie klikami w naglowki tabeli - stan trzymany tutaj (nie w DOM), bo
// tabela jest w calosci podmieniana przy kazdym renderPortfolio() (odswiezenie
// z T212) - currentPositions/currentTotals to dane z OSTATNIEGO udanego
// odswiezenia, zeby klik sortujacy mial z czego sortowac bez ponownego
// zapytania do T212, i zeby wybrany sort PRZETRWAL kolejne auto-odswiezenia.
let currentPositions = null;
let currentTotals = { value: 0, ppl: 0, pplPct: 0 };
const sortState = { key: null, dir: 1 };

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
}

function pplClass(value) {
    return value >= 0 ? "focus-tile__pnl--profit" : "focus-tile__pnl--loss";
}

function sortHeaderCell(label, key) {
    const active = sortState.key === key;
    const arrow = active ? (sortState.dir === 1 ? "▲" : "▼") : "⇅";
    return `<th class="history-table__th--sortable${active ? " history-table__th--sortable--active" : ""}" data-sort-key="${key}">${label} <span class="sort-arrow">${arrow}</span></th>`;
}

function renderPortfolio(positions, totalValue, totalPpl, totalPplPct) {
    const container = document.getElementById("portfolio-content");
    if (!container) return;

    if (!positions.length) {
        container.innerHTML = '<p class="auth-box__hint">Brak otwartych pozycji.</p>';
        return;
    }

    const rows = positions.map((p) => {
        const avatar = p.logo_filename
            ? `<img class="pie-asset-row__avatar" style="width:28px;height:28px" src="/static/logos/${encodeURIComponent(p.logo_filename)}" alt="${escapeHtml(p.display_ticker)}">`
            : `<span class="pie-asset-row__avatar" style="width:28px;height:28px;font-size:12px;background: hsl(${p.hue}, 55%, 38%);">${escapeHtml(p.initial)}</span>`;

        const marketDot = p.market_open === null || p.market_open === undefined
            ? ""
            : `<span class="market-dot ${p.market_open ? "market-dot--open" : "market-dot--closed"}" title="${p.market_open ? "Giełda otwarta" : "Giełda zamknięta"}"></span>`;

        return `
            <tr class="${p.ppl >= 0 ? "portfolio-row--profit" : "portfolio-row--loss"}">
                <td>
                    <a href="/instrument/${encodeURIComponent(p.ticker)}" class="pie-asset-row__ticker-link">
                        ${avatar}
                        <span>
                            <span class="pie-asset-row__ticker">${marketDot}${escapeHtml(p.name || p.display_ticker)}</span>
                            <span class="pie-asset-row__currency">${escapeHtml(p.display_ticker)}${p.currency ? " · " + `<span class="currency-badge currency-badge--${escapeHtml(p.currency.toLowerCase())}">${escapeHtml(p.currency)}</span>` : ""}</span>
                        </span>
                    </a>
                </td>
                <td>${p.quantity}</td>
                <td>${p.avg_price.toFixed(2)}</td>
                <td>${p.current_price.toFixed(2)}</td>
                <td>${p.value.toFixed(2)}</td>
                <td class="${pplClass(p.ppl)}">
                    ${p.ppl >= 0 ? "+" : ""}${p.ppl.toFixed(2)}
                    (${p.ppl_pct >= 0 ? "+" : ""}${p.ppl_pct.toFixed(1)}%)
                </td>
                <td class="portfolio-bot-cell" data-ticker="${escapeHtml(p.ticker)}">
                    ${p.bot_managed
                        ? `<span class="badge badge--bot">Zarządzane przez bota</span>
                           <button type="button" class="account-bar__btn portfolio-release-btn" data-trade-id="${p.bot_trade_id}">Cofnij</button>`
                        : `<button type="button" class="account-bar__btn portfolio-adopt-btn" data-ticker="${escapeHtml(p.ticker)}" data-on-bot-list="${p.on_bot_list ? "true" : "false"}">Przekaż botowi</button>`}
                </td>
            </tr>`;
    }).join("");

    container.innerHTML = `
        <div class="portfolio-summary portfolio-summary--pulse">
            <div class="portfolio-summary__cell">
                <span class="instrument-detail__position-label">Wartość portfela</span>
                <span class="instrument-detail__position-value">${totalValue.toFixed(2)}</span>
                <span class="portfolio-freshness portfolio-freshness--live">Aktualne</span>
            </div>
            <div class="portfolio-summary__cell">
                <span class="instrument-detail__position-label">Zysk / strata</span>
                <span class="instrument-detail__position-value ${pplClass(totalPpl)}">
                    ${totalPpl >= 0 ? "+" : ""}${totalPpl.toFixed(2)}
                    (${totalPplPct >= 0 ? "+" : ""}${totalPplPct.toFixed(1)}%)
                </span>
            </div>
        </div>
        <table class="history-table" id="portfolio-table">
            <thead>
                <tr>
                    ${sortHeaderCell("Aktywo", "name")}
                    ${sortHeaderCell("Ilość", "quantity")}
                    ${sortHeaderCell("Średnia cena", "avg_price")}
                    ${sortHeaderCell("Cena teraz", "current_price")}
                    ${sortHeaderCell("Wartość", "value")}
                    ${sortHeaderCell("Zysk / strata", "ppl")}
                    <th>Bot</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

/*
Sortuje currentPositions (dane z ostatniego udanego /warp/portfolio/refresh)
wg biezacego sortState i renderuje - NIE dotyka sortState, wywolujacy
odpowiada za jego ustawienie. "name" sortuje alfabetycznie, reszta kluczy
numerycznie. Woluje ja zarowno klik w naglowek (sortPositions ponizej) jak i
refreshPortfolio() (zeby zachowac wybrany sort po auto-odswiezeniu).
*/
function renderSorted() {
    let positions = currentPositions;
    if (sortState.key) {
        const key = sortState.key;
        positions = currentPositions.slice().sort((a, b) => {
            if (key === "name") {
                const av = (a.name || a.display_ticker).toLowerCase();
                const bv = (b.name || b.display_ticker).toLowerCase();
                return av < bv ? -sortState.dir : av > bv ? sortState.dir : 0;
            }
            return (a[key] - b[key]) * sortState.dir;
        });
    }
    renderPortfolio(positions, currentTotals.value, currentTotals.ppl, currentTotals.pplPct);
}

/*
Klik w naglowek (patrz sortHeaderCell wyzej) - ten sam klucz klikniety
ponownie odwraca kierunek, inny klucz resetuje na rosnaco.
*/
function sortPositions(key) {
    if (!currentPositions) return;  // przed pierwszym udanym odswiezeniem - nie ma jeszcze czego sortowac

    if (sortState.key === key) {
        sortState.dir *= -1;
    } else {
        sortState.key = key;
        sortState.dir = 1;
    }
    renderSorted();
}

/*
"Przekaz botowi" / "Cofnij" (patrz routes/bot.py::adopt_position/release_position,
pomysl #2 z docs/IDEAS_v2.md + jego odwrotnosc) - delegacja zdarzen na
#portfolio-content, bo wiersze sa podmieniane w calosci przy kazdym
renderPortfolio() (odswiezenie z T212), a sam kontener zostaje ten sam
element przez cala zywotnosc strony. confirmDialog - ten sam modal co
bot.js::unblock (common.js), zamiast window.confirm, dla spojnosci wygladu.
*/
async function adoptPosition(ticker, onBotList) {
    let entryAmount = null;
    if (!onBotList) {
        const input = window.prompt(
            `${ticker} nie jest jeszcze na liście bota - podaj kwotę wejścia (na przyszłe poziomy DCA):`
        );
        if (input === null) return;
        entryAmount = input.trim();
        if (!entryAmount) return;
    }

    if (!(await confirmDialog(`Przekazać ${ticker} botowi? Od tego momentu bot przejmuje zarządzanie WYJŚCIEM z tej pozycji (trailing stop).`))) {
        return;
    }

    try {
        const resp = await fetch("/bot/asset/adopt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker, entry_amount: entryAmount }),
        });
        const data = await resp.json();
        if (!data.ok) {
            playError();
            window.alert(`Nie udało się przekazać ${ticker} botowi: ${data.error}`);
            return;
        }
        playSuccess();
        refreshPortfolio();
    } catch (err) {
        playError();
        console.error("Błąd adopcji pozycji:", err);
        window.alert("Błąd sieci przy przekazywaniu pozycji botowi.");
    }
}

async function releasePosition(tradeId) {
    if (!(await confirmDialog("Cofnąć tę pozycję spod zarządzania bota? Udziały ZOSTAJĄ na koncie - tylko bot przestaje ich pilnować (trailing STOP zostanie anulowany)."))) {
        return;
    }

    try {
        const resp = await fetch(`/bot/positions/${tradeId}/release`, { method: "POST" });
        const data = await resp.json();
        if (!data.ok) {
            playError();
            window.alert(`Nie udało się cofnąć pozycji: ${data.error}`);
            return;
        }
        playSuccess();
        refreshPortfolio();
    } catch (err) {
        playError();
        console.error("Błąd zwalniania pozycji:", err);
        window.alert("Błąd sieci przy cofaniu pozycji.");
    }
}

document.getElementById("portfolio-content")?.addEventListener("click", (ev) => {
    const adoptBtn = ev.target.closest(".portfolio-adopt-btn");
    if (adoptBtn) {
        adoptPosition(adoptBtn.dataset.ticker, adoptBtn.dataset.onBotList === "true");
        return;
    }
    const releaseBtn = ev.target.closest(".portfolio-release-btn");
    if (releaseBtn) {
        releasePosition(releaseBtn.dataset.tradeId);
        return;
    }
    const sortTh = ev.target.closest("[data-sort-key]");
    if (sortTh) {
        sortPositions(sortTh.dataset.sortKey);
    }
});

/*
Zamiast tekstowego komunikatu bledu (usuniete na zyczenie Adama, 20.07.2026) -
znacznik swiezosci danych POD wartoscia portfela (.portfolio-freshness,
patrz portfolio.html): czerwony "Z cache" domyslnie (tak renderuje go Jinja
od razu przy pierwszym wczytaniu strony), zielony "Aktualne" dopiero po
udanym zywym odswiezeniu (renderPortfolio() wyzej). Przy nieudanym
odswiezeniu (najczesciej 429 - rate limit T212 demo) po prostu NIC sie nie
zmienia - znacznik zostaje czerwony, bo dane faktycznie nadal sa z cache.
*/
async function refreshPortfolio() {
    try {
        const resp = await fetch("/warp/portfolio/refresh");
        const data = await resp.json();

        if (!data.ok) {
            console.warn("Odswiezenie portfela nie powiodlo sie:", data.error);
            return;
        }

        currentPositions = data.positions;
        currentTotals = { value: data.total_value, ppl: data.total_ppl, pplPct: data.total_ppl_pct };

        renderSorted();  // zachowuje wybrany sort (jesli user juz kliknal jakis naglowek) zamiast wracac do domyslnej kolejnosci z backendu
        playUpdate();
    } catch (err) {
        console.error("Błąd odświeżania portfela:", err);
    }
}

setTimeout(refreshPortfolio, REFRESH_DELAY_MS);
