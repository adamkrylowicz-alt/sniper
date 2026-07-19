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

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
}

function pplClass(value) {
    return value >= 0 ? "focus-tile__pnl--profit" : "focus-tile__pnl--loss";
}

function renderPortfolio(positions, totalValue, totalPpl) {
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

        return `
            <tr>
                <td>
                    <a href="/instrument/${encodeURIComponent(p.ticker)}" class="pie-asset-row__ticker-link">
                        ${avatar}
                        <span>
                            <span class="pie-asset-row__ticker">${escapeHtml(p.name || p.display_ticker)}</span>
                            <span class="pie-asset-row__currency">${escapeHtml(p.display_ticker)}${p.currency ? " · " + escapeHtml(p.currency) : ""}</span>
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
                </span>
            </div>
        </div>
        <table class="history-table">
            <thead>
                <tr>
                    <th>Aktywo</th>
                    <th>Ilość</th>
                    <th>Średnia cena</th>
                    <th>Cena teraz</th>
                    <th>Wartość</th>
                    <th>Zysk / strata</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

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

        renderPortfolio(data.positions, data.total_value, data.total_ppl);
        playUpdate();
    } catch (err) {
        console.error("Błąd odświeżania portfela:", err);
    }
}

setTimeout(refreshPortfolio, REFRESH_DELAY_MS);
