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
// Zapisywany tez do localStorage (SORT_STORAGE_KEY), zeby przetrwal PELNE
// przeladowanie strony (F5) - bez tego po odswiezeniu wracalo zawsze do
// domyslnej kolejnosci z backendu (wartosc malejaco), mimo ze user wybral
// inny sort przed chwila (zgloszone przez Adama, 27.07.2026).
const SORT_STORAGE_KEY = "snajper-portfolio-sort";
let currentPositions = null;
let currentTotals = { value: 0, ppl: 0, pplPct: 0 };
const sortState = { key: null, dir: 1 };
try {
    const saved = JSON.parse(localStorage.getItem(SORT_STORAGE_KEY));
    if (saved && saved.key) {
        sortState.key = saved.key;
        sortState.dir = saved.dir === -1 ? -1 : 1;
    }
} catch (err) {
    // localStorage niedostepny/uszkodzony wpis - zostaje domyslny brak sortu
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
}

function pplClass(value) {
    return value >= 0 ? "focus-tile__pnl--profit" : "focus-tile__pnl--loss";
}

// "bot"/"signal"/"eod" -> etykieta w UI, jeden na wszystkie 3 przyciski
// adopcji + odznaka "Zarządzane przez X" (patrz routes/scalping.py::
// _annotate_bot_state, managed_by - 2026-08-04, "ujednolić wszystkie boty").
const ENGINE_LABELS = { bot: "Micro-Grid", signal: "Sygnał", eod: "EOD" };
function engineLabel(engine) {
    return ENGINE_LABELS[engine] || engine;
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
                <td>${p.currency ? `<span class="currency-badge currency-badge--${escapeHtml(p.currency.toLowerCase())}">${escapeHtml(p.currency)}</span>` : ""}</td>
                <td>${p.quantity}</td>
                <td>${p.avg_price.toFixed(2)}</td>
                <td>${p.current_price.toFixed(2)}</td>
                <td>${p.value.toFixed(2)}</td>
                <td class="${pplClass(p.ppl)}">
                    ${p.ppl >= 0 ? "+" : ""}${p.ppl.toFixed(2)}
                    (${p.ppl_pct >= 0 ? "+" : ""}${p.ppl_pct.toFixed(1)}%)
                </td>
                <td class="portfolio-bot-cell" data-ticker="${escapeHtml(p.ticker)}">
                    ${p.managed_by
                        ? `<span class="badge badge--bot">Zarządzane przez ${engineLabel(p.managed_by)}</span>
                           <button type="button" class="account-bar__btn portfolio-release-btn" data-engine="${p.managed_by}" data-trade-id="${p.managed_trade_id}">Cofnij</button>`
                        : `<div class="portfolio-adopt-group">
                               <button type="button" class="account-bar__btn portfolio-adopt-btn" data-engine="bot" data-ticker="${escapeHtml(p.ticker)}" data-on-list="${p.on_bot_list ? "true" : "false"}">→ Micro-Grid</button>
                               <button type="button" class="account-bar__btn portfolio-adopt-btn" data-engine="signal" data-ticker="${escapeHtml(p.ticker)}" data-on-list="${p.on_signal_list ? "true" : "false"}">→ Sygnał</button>
                               <button type="button" class="account-bar__btn portfolio-adopt-btn" data-engine="eod" data-ticker="${escapeHtml(p.ticker)}" data-on-list="${p.on_eod_list ? "true" : "false"}">→ EOD</button>
                           </div>`}
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
                    ${sortHeaderCell("Waluta", "currency")}
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
            if (key === "currency") {
                const av = (a.currency || "").toLowerCase();
                const bv = (b.currency || "").toLowerCase();
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
    try {
        localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify({ key: sortState.key, dir: sortState.dir }));
    } catch (err) {
        // localStorage niedostepny (np. tryb prywatny) - sort dziala, po prostu nie przetrwa F5
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
async function adoptPosition(engine, ticker, onList) {
    let entryAmount = null;
    if (!onList) {
        const input = window.prompt(
            `${ticker} nie jest jeszcze na liście ${engineLabel(engine)} - podaj kwotę wejścia (na przyszłe poziomy DCA):`
        );
        if (input === null) return;
        entryAmount = input.trim();
        if (!entryAmount) return;
    }

    if (!(await confirmDialog(`Przekazać ${ticker} silnikowi ${engineLabel(engine)}? Od tego momentu przejmuje zarządzanie WYJŚCIEM z tej pozycji (trailing stop).`))) {
        return;
    }

    try {
        const resp = await fetch(`/${engine}/asset/adopt`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker, entry_amount: entryAmount }),
        });
        const data = await resp.json();
        if (!data.ok) {
            playError();
            window.alert(`Nie udało się przekazać ${ticker} silnikowi ${engineLabel(engine)}: ${data.error}`);
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

async function releasePosition(engine, tradeId) {
    if (!(await confirmDialog(`Cofnąć tę pozycję spod zarządzania ${engineLabel(engine)}? Udziały ZOSTAJĄ na koncie - tylko bot przestaje ich pilnować (trailing STOP zostanie anulowany).`))) {
        return;
    }

    try {
        const resp = await fetch(`/${engine}/positions/${tradeId}/release`, { method: "POST" });
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
        adoptPosition(adoptBtn.dataset.engine, adoptBtn.dataset.ticker, adoptBtn.dataset.onList === "true");
        return;
    }
    const releaseBtn = ev.target.closest(".portfolio-release-btn");
    if (releaseBtn) {
        releasePosition(releaseBtn.dataset.engine, releaseBtn.dataset.tradeId);
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

/*
Dane z cache (embedowane przez portfolio_view() w portfolio.html, patrz
scalping.py::_serialize_portfolio) - naniesione na tabele NATYCHMIAST (bez
czekania na REFRESH_DELAY_MS), zeby zapamietany sort (SORT_STORAGE_KEY)
zadzialal od razu i tabela nie "skakala" z kolejnosci backendu na sort usera
dopiero po sieciowym odswiezeniu (zgloszone przez Adama, 27.07.2026 - widoczny
rozjazd tuz po zaladowaniu strony). Jesli sortState.key jest puste, sort i tak
nie zmienia kolejnosci (identyczna z ta co juz wyrenderowal Jinja), wiec
re-render jest wtedy nieszkodliwym no-opem wizualnie.
*/
const initialDataEl = document.getElementById("portfolio-initial-data");
if (initialDataEl) {
    try {
        const initial = JSON.parse(initialDataEl.textContent);
        currentPositions = initial.positions;
        currentTotals = { value: initial.total_value, ppl: initial.total_ppl, pplPct: initial.total_ppl_pct };
        renderSorted();
    } catch (err) {
        console.error("Błąd odczytu wstępnych danych portfela:", err);
    }
}

setTimeout(refreshPortfolio, REFRESH_DELAY_MS);
