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
// z /warp/trade_levels - [{type: "stop_loss"|"take_profit"|"pending_buy", price, source,
// order_id?, editable?}] - order_id/editable tylko dla pending_buy (dodane 2026-07-30).
let tradeLevels = [];
let overlayPriceLines = []; // wszystkie linie aktualnie narysowane na candleSeries (srednia + SL/TP + pending_buy)
let livePriceLine = null; // osobna od overlayPriceLines - odswiezana co 5s, nie chcemy migotac reszty linii tak czesto

// Przeciaganie linii oczekujacych zlecen LIMIT BUY (dodane 2026-07-30, Adam:
// "chce zeby mozna bylo... lapiac za kreske i przesuwajac ja po wykresie").
// TYLKO dla wpisow pending_buy z editable=true (nie-bota, patrz
// scalping.py::_bot_order_sources) - reszta linii nie ma tu zadnego wpisu,
// wiec mousedown w ich poblizu nic nie robi.
let draggablePendingBuys = []; // [{line, orderId, price}] - ilosc/ticker rozwiazywane server-side w /reprice
let dragState = null; // {entry, startPrice} podczas aktywnego przeciagania, inaczej null
let repricePopoverEl = null;
let dragPriceLabelEl = null; // zywa etykieta ceny podazajaca za kursorem w trakcie przeciagania
// Prawdziwy bug (2026-07-30, Adam: "na ekranie byl chuj wielki i 2 babelki"):
// klik "Zatwierdz" odpala commitReprice() (request sieciowy) + potem
// loadTradeLevels() (przebudowuje WSZYSTKIE linie/draggablePendingBuys z
// serwera). Jesli w tym czasie (kilkaset ms) user zdazyl zlapac za INNA
// linie i ja przeciagnac, dragState.entry.line wskazywal juz na obiekt
// usuniety przez updateOverlayLines() - duch, ktory nadal "reagowal" na
// mousemove/mouseup i tworzyl WLASNY popover obok tego ze świeżo
// przeladowanych danych = 2 bąbelki + krzaczaca sie linia. Blokujemy nowe
// przeciaganie na czas trwania zatwierdzania (do konca loadTradeLevels()).
let repriceInFlight = false;
const DRAG_HIT_TOLERANCE_PX = 6;

function themeColor(varName) {
    return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

function addOverlayLine(price, color, style, width, title) {
    if (!candleSeries || price == null) return null;
    const line = candleSeries.createPriceLine({
        price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title,
    });
    overlayPriceLines.push(line);
    return line;
}

// Zywa linia aktualnej ceny (dodane 2026-07-28, Adam po zauwazeniu ze swiece
// dzienne dla tickerow EUR (np. Allianz) potrafia byc kilka dni nieaktualne -
// Yahoo, darmowe/nieoficjalne zrodlo, czasem po prostu jeszcze nie publikuje
// swiecy za dzisiaj dla danej spolki, sprawdzone bezposrednio z pominieciem
// cache'a: ta sama, nieaktualna swieca). "pewnie dopoki nie bedzie danych z
// IBKR" - tymczasowe obejscie: prosta, zywa poprzeczka na TEJ SAMEJ cenie co
// bot uzywa do decyzji (/warp/quote, ten sam price_feed co bot_engine.py),
// wiec nawet gdy swiece sa stare, widac gdzie NAPRAWDE jest cena teraz.
// Odswiezana co 5s razem z refreshQuote() - CELOWO osobna od
// overlayPriceLines (srednia/SL/TP), ktore migotalyby bez potrzeby przy tak
// czestym odswiezaniu.
function updateLivePriceLine(price) {
    if (!candleSeries || price == null) return;
    if (livePriceLine) {
        candleSeries.removePriceLine(livePriceLine);
        livePriceLine = null;
    }
    livePriceLine = candleSeries.createPriceLine({
        price,
        color: themeColor("--text-bright"),
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: "Cena teraz",
    });
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
    draggablePendingBuys = [];
    hideRepricePopover();
    hideDragPriceLabel();
    dragState = null; // linie ponizej przebudowane od zera - stary obiekt linii juz nie istnieje

    if (heldAveragePrice) {
        const color = heldPpl >= 0 ? themeColor("--buy-green") : themeColor("--sell-red");
        addOverlayLine(heldAveragePrice, color, LightweightCharts.LineStyle.Dashed, 2, "Twoja średnia");
    }
    tradeLevels.forEach((lvl) => {
        if (lvl.type === "stop_loss") {
            addOverlayLine(lvl.price, themeColor("--sell-red"), LightweightCharts.LineStyle.Dotted, 1, `Stop-loss (${lvl.source})`);
        } else if (lvl.type === "take_profit") {
            addOverlayLine(lvl.price, themeColor("--accent-amber"), LightweightCharts.LineStyle.Dotted, 1, `Take-profit (${lvl.source})`);
        } else if (lvl.type === "pending_buy") {
            // Dodane 2026-07-30 (Adam: "chce zeby tez byla taka kreska jak
            // odpale np buy limit order recznie") - LargeDashed odroznia
            // wizualnie od Dashed (srednia) i Dotted (SL/TP). Przeciaganie
            // (bindDragHandlers) dziala WYLACZNIE dla editable=true (nie-bota,
            // patrz scalping.py::_bot_order_sources) - zlecenia bota rysuja
            // sie identycznie, ale bez wpisu w draggablePendingBuys, wiec
            // mousedown w ich poblizu nic nie robi.
            const line = addOverlayLine(
                lvl.price, themeColor("--accent-blue"), LightweightCharts.LineStyle.LargeDashed, 1,
                `Kupno LIMIT (${lvl.source})`,
            );
            if (lvl.editable && line) {
                draggablePendingBuys.push({ line, orderId: lvl.order_id, price: lvl.price, source: lvl.source });
            }
        }
    });
}

// -- Przeciaganie linii pending_buy (edytowalnych zlecen LIMIT BUY) --------

function hideRepricePopover() {
    if (repricePopoverEl) {
        repricePopoverEl.remove();
        repricePopoverEl = null;
    }
}

function updateDragPriceLabel(y, price, container) {
    if (!dragPriceLabelEl) {
        dragPriceLabelEl = document.createElement("div");
        dragPriceLabelEl.className = "chart-drag-price-label";
        container.appendChild(dragPriceLabelEl);
    }
    dragPriceLabelEl.style.top = `${Math.max(0, y - 11)}px`;
    // Ostrzezenie na biezaco w trakcie przeciagania (Adam: "daj warna jak
    // wyjedzie za wysoko") - >= cena rynkowa = wykona sie NATYCHMIAST jak
    // zwykle kupno, nie zostanie oczekujace (patrz tez confirm() przy
    // zatwierdzaniu w showRepricePopover).
    const marketPrice = lastQuote && lastQuote.c != null ? Number(lastQuote.c) : null;
    const crossesMarket = marketPrice != null && price >= marketPrice;
    dragPriceLabelEl.classList.toggle("chart-drag-price-label--warn", crossesMarket);
    dragPriceLabelEl.textContent = crossesMarket ? `${price.toFixed(4)} ⚠ kupi po rynku` : price.toFixed(4);
}

function hideDragPriceLabel() {
    if (dragPriceLabelEl) {
        dragPriceLabelEl.remove();
        dragPriceLabelEl = null;
    }
}

function isMarketCrossing(price) {
    return lastQuote != null && lastQuote.c != null && price >= Number(lastQuote.c);
}

// Przelacza wyglad linii pending_buy miedzy "LIMIT" (niebieska, przerywana)
// a "MARKET" (czerwona, ciagla) na biezaco w trakcie przeciagania (Adam,
// 2026-07-30: "jak wjedzie wyzej zmien na market bo dalej swieci limit") -
// sama etykieta ostrzegawcza przy kursorze (updateDragPriceLabel) nie
// wystarczala, tytul/kolor SAMEJ linii tez musial sie zmienic, bo to ona
// zostaje widoczna na wykresie (dymek znika po puszczeniu myszy).
function applyDragLineAppearance(entry, price) {
    const crosses = isMarketCrossing(price);
    entry.line.applyOptions({
        price,
        color: themeColor(crosses ? "--sell-red" : "--accent-blue"),
        lineStyle: crosses ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.LargeDashed,
        title: crosses ? `Kupno MARKET, natychmiast (${entry.source})` : `Kupno LIMIT (${entry.source})`,
    });
    entry.price = price;
}

async function commitReprice(entry) {
    try {
        const resp = await fetch(`/warp/order/${encodeURIComponent(entry.orderId)}/reprice`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ new_price: entry.price }),
        });
        const data = await resp.json();
        if (!data.ok) {
            alert(data.error || data.reason || "Nie udało się zmienić ceny zlecenia.");
            return false;
        }
        return true;
    } catch (err) {
        console.error("Błąd repricingu zlecenia:", err);
        alert("Błąd połączenia przy zmianie ceny zlecenia.");
        return false;
    }
}

async function commitCancelOrder(entry) {
    try {
        const resp = await fetch(`/warp/order/${encodeURIComponent(entry.orderId)}/cancel`, { method: "POST" });
        const data = await resp.json();
        if (!data.ok) {
            alert(data.error || data.reason || "Nie udało się skasować zlecenia.");
            return false;
        }
        return true;
    } catch (err) {
        console.error("Błąd kasowania zlecenia:", err);
        alert("Błąd połączenia przy kasowaniu zlecenia.");
        return false;
    }
}

function showRepricePopover(entry, startPrice, container) {
    hideRepricePopover();
    const y = candleSeries.priceToCoordinate(entry.price);
    if (y == null) return;

    // Guzik "Skasuj zlecenie" - ZAWSZE widoczny (poprawione 2026-07-30:
    // wczesniej ukrywany gdy cena >= rynek, na blednym zalozeniu ze taka
    // cena i tak wykona sie natychmiast jak market. Obalone na zywo -
    // zlecenie KO_US_EQ @ 90.14 (rynek 88.49) stalo realnie OCZEKUJACE
    // 10+ minut, nigdy sie nie wykonalo - user nie mogl go skasowac, bo
    // przycisk po prostu znikal. isMarketCrossing() to niepewna heurystyka
    // z WLASNEGO price feedu (Alpaca/Finnhub/Yahoo), nie prawdziwy stan
    // ksiegi T212 - nie wolno na niej ukrywac akcji, ktora moze byc
    // jedynym sposobem pozbycia sie zlecenia).
    const el = document.createElement("div");
    el.className = "chart-reprice-popover";
    el.style.top = `${Math.max(0, y - 14)}px`;
    el.style.right = "44px";
    el.innerHTML = `
        <span class="chart-reprice-popover__price">${entry.price.toFixed(4)}</span>
        <button type="button" class="pending-orders-list__action-btn pending-orders-list__action-btn--confirm" data-action="confirm">Zatwierdź</button>
        <button type="button" class="pending-orders-list__action-btn pending-orders-list__action-btn--cancel" data-action="cancel-order">Skasuj</button>
        <button type="button" class="pending-orders-list__action-btn" data-action="cancel">Anuluj</button>
    `;
    // KLUCZOWY fix (2026-07-30, Adam: "kliknalem i chuja sie dzieje" - zero
    // logow w konsoli nawet dla samego kliknięcia): popover jest DZIECKIEM
    // TEGO SAMEGO kontenera co sam wykres (container.appendChild(el) nizej),
    // a bindDragHandlers() wiesza wlasny "mousedown" na CALYM kontenerze do
    // wykrywania przeciagania linii. Bez zatrzymania propagacji, kazdy klik
    // w przycisk popovera (Skasuj/Zatwierdz/Anuluj) najpierw przechodzil
    // przez ten handler kontenera - w praktyce nie blokowal (entry=null ->
    // return), ale prawdopodobnie interferowal z biblioteka wykresu
    // (Lightweight Charts tez nasluchuje mousedown na tym samym elemencie
    // do pan/zoom) na tyle, ze klik nigdy nie docieral do faktycznego
    // <button>. stopPropagation() na mousedown popovera gwarantuje ze
    // zaden z tych zewnetrznych handlerow go juz nie zobaczy.
    el.addEventListener("mousedown", (e) => e.stopPropagation());
    // Potwierdzenie "kliknij drugi raz" ZAMIAST natywnego confirm() (Adam,
    // 2026-07-30: "nic nie znikało po prostu nie działało") - podejrzenie:
    // natywne okienko confirm() w polskiej przegladarce ma przyciski
    // "OK"/"Anuluj", a w TYM SAMYM popoverze jest tez wlasny przycisk
    // "Anuluj" (o zupelnie innym znaczeniu - cofniecie przeciagniecia) -
    // latwo kliknac nie ten przycisk i nie zauwazyc, ze cala akcja po cichu
    // sie nie wykonala. Bez osobnego okienka nie ma tej dwuznacznosci.
    function armTwoStep(btn, armedLabel) {
        let armed = false;
        let timer = null;
        const originalLabel = btn.textContent;
        return {
            isArmed: () => armed,
            disarm: () => {
                armed = false;
                clearTimeout(timer);
                btn.textContent = originalLabel;
                btn.classList.remove("pending-orders-list__action-btn--armed");
            },
            arm: () => {
                armed = true;
                btn.textContent = armedLabel;
                btn.classList.add("pending-orders-list__action-btn--armed");
                timer = setTimeout(() => {
                    armed = false;
                    btn.textContent = originalLabel;
                    btn.classList.remove("pending-orders-list__action-btn--armed");
                }, 4000);
            },
        };
    }

    const confirmBtn = el.querySelector('[data-action="confirm"]');
    const confirmArm = armTwoStep(confirmBtn, "Kupi PO RYNKU - kliknij ponownie");
    confirmBtn.addEventListener("click", async () => {
        // Ostrzezenie (Adam: "daj warna jak wyjedzie za wysoko ze kupno
        // market") - LIMIT BUY z cena >= aktualnej ceny rynkowej wykona sie
        // NATYCHMIAST jak zwykle kupno (patrz realny przypadek 30.07 - 0,2
        // szt. KO kupione po 88,35 zamiast zostac oczekujace). Wymaga
        // DRUGIEGO klikniecia zamiast confirm() - patrz komentarz wyzej.
        if (isMarketCrossing(entry.price) && !confirmArm.isArmed()) {
            confirmArm.arm();
            return;
        }
        confirmArm.disarm();
        el.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        repriceInFlight = true;
        try {
            const ok = await commitReprice(entry);
            hideRepricePopover();
            if (ok) {
                await loadTradeLevels(); // odswiez z serwera - nowy order_id po anuluj+zloz-nowe
            } else {
                applyDragLineAppearance(entry, startPrice);
            }
        } finally {
            repriceInFlight = false;
        }
    });

    // Skasuj = JEDEN klik, bez potwierdzenia (Adam, 2026-07-30: "kasuje i
    // nic się nie dzieje kumasz?" - podwojne kliknicie do potwierdzenia
    // bylo kolejnym zrodlem "nic sie nie dzieje", user nie zauwazal zmiany
    // tekstu na przycisku po pierwszym kliknieciu). Niskie ryzyko pomylki -
    // w najgorszym razie trzeba zlozyc zlecenie ponownie, w odroznieniu od
    // przypadkowego KUPNA po rynku (Zatwierdz przy market-crossing), gdzie
    // podwojne kliknięcie NADAL obowiazuje.
    const cancelOrderBtn = el.querySelector('[data-action="cancel-order"]');
    cancelOrderBtn.addEventListener("click", async () => {
        el.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        repriceInFlight = true;
        try {
            const ok = await commitCancelOrder(entry);
            hideRepricePopover();
            if (ok) {
                await loadTradeLevels(); // odswiez z serwera - linia zniknie, zlecenie faktycznie skasowane
            } else {
                applyDragLineAppearance(entry, startPrice);
            }
        } finally {
            repriceInFlight = false;
        }
    });
    el.querySelector('[data-action="cancel"]').addEventListener("click", () => {
        applyDragLineAppearance(entry, startPrice);
        hideRepricePopover();
    });

    container.appendChild(el);
    repricePopoverEl = el;
}

function bindDragHandlers(container) {
    container.addEventListener("mousedown", (event) => {
        if (dragState || !candleSeries || repriceInFlight) return;
        const rect = container.getBoundingClientRect();
        const y = event.clientY - rect.top;
        const entry = draggablePendingBuys.find((e) => {
            const lineY = candleSeries.priceToCoordinate(e.price);
            return lineY != null && Math.abs(lineY - y) <= DRAG_HIT_TOLERANCE_PX;
        });
        if (!entry) return;
        event.preventDefault();
        hideRepricePopover();
        dragState = { entry, startPrice: entry.price };
        container.style.cursor = "ns-resize";
        updateDragPriceLabel(y, entry.price, container);
    });

    document.addEventListener("mousemove", (event) => {
        if (!dragState || !candleSeries) return;
        const rect = container.getBoundingClientRect();
        const y = event.clientY - rect.top;
        const newPrice = candleSeries.coordinateToPrice(y);
        if (newPrice == null || newPrice <= 0) return;
        applyDragLineAppearance(dragState.entry, newPrice);
        updateDragPriceLabel(y, newPrice, container);
    });

    document.addEventListener("mouseup", () => {
        if (!dragState) return;
        container.style.cursor = "";
        hideDragPriceLabel();
        const { entry, startPrice } = dragState;
        dragState = null;
        if (Math.abs(entry.price - startPrice) < 1e-9) return; // brak realnej zmiany
        showRepricePopover(entry, startPrice, container);
    });
}

async function loadTradeLevels() {
    try {
        const resp = await fetch(`/warp/trade_levels?ticker=${encodeURIComponent(ticker)}`);
        const data = await resp.json();
        if (!data.ok) return;
        if (data.pending_unavailable) {
            // Backend nie zdolal sprawdzic oczekujacych zlecen teraz (429/
            // cache bledu T212, patrz scalping.py::trade_levels) - NIE
            // kasujemy dotychczasowej linii "Kupno LIMIT", tylko dokladamy
            // do niej swiezo pobrane SL/TP. Bez tego linia znikala losowo
            // przy kazdym odswiezeniu strony, mimo ze zlecenie realnie
            // istnialo na T212 (Adam, 2026-07-30: "NIE MA ZADNEGO").
            const oldPendingBuys = tradeLevels.filter((lvl) => lvl.type === "pending_buy");
            tradeLevels = [...(data.levels || []), ...oldPendingBuys];
        } else {
            tradeLevels = data.levels || [];
        }
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
        // Domyslne wskazniki biblioteki "ostatnia cena" - DWIE OSOBNE opcje,
        // obie wlaczone domyslnie (latwo przeoczyc, ze to nie jedna rzecz):
        // lastValueVisible = etykieta/tag na osi, priceLineVisible = sama
        // poprzeczna kreska na wykresie. WYLACZONE OBIE 2026-07-30 (Adam:
        // pierwszy fix lastValueVisible usunal tylko etykiete, ale kreska
        // sama w sobie (priceLineVisible) zostala i dalej nakladala sie
        // wizualnie na WLASNA linie "Cena teraz" - patrz screen asml2.png).
        // Obie pokazuja cene zamkniecia OSTATNIEJ SWIECY (na wykresie 5min
        // moze byc kilka minut nieaktualna), nie live cene - mamy juz WLASNA,
        // opisana linia "Cena teraz" (updateLivePriceLine, odswiezana co 5s
        // z /warp/quote), biblioteczne byly zbedne i wprowadzaly w blad.
        lastValueVisible: false,
        priceLineVisible: false,
    });
    bindDragHandlers(container);
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
// to np. "1min", a AKTUALNY ticker nie ma pokrycia intraday (wiec ta
// zakladka w ogole nie istnieje na tej stronie - patrz {% if
// has_intraday_chart %} w szablonie, US zawsze/EU tylko z
// price_feed.IBKR_TICKER_MAP) - cichy fallback na domyslnie aktywna
// zakladke w HTML (1M), zamiast bledu/pustego wykresu.
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
    // routes/scalping.py::candles ?interval=1m - US zawsze przez Alpaca,
    // EU tylko tickery z price_feed.IBKR_TICKER_MAP).
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
        updateLivePriceLine(lastQuote.c);
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

// Tryb zlecenia RYNEK/LIMIT - przelacznik zamiast osobnych przyciskow
// (Adam, 2026-07-31: "zrob po prostu switch market/limit a guziki zostaw
// te same") - poprzednia wersja z osobnym drugim rzedem "LIMIT KUP"/
// "LIMIT SPRZEDAJ" psula layout kafelka. Teraz TE SAME Kup/Sprzedaj
// przyciski ponizej wysylaja MARKET albo LIMIT zaleznie od orderMode.
let orderMode = "market";

async function sendOrder(side) {
    const quantity = getQuantity(side);
    const statusEl = document.getElementById("instrument-status");
    const btns = document.querySelectorAll(".focus-tile__btn");
    const limitPriceInput = document.getElementById("instrument-limit-price");

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

    const marketPrice = lastQuote && lastQuote.c ? Number(lastQuote.c) : null;
    let limitPrice = null;
    if (orderMode === "limit") {
        limitPrice = limitPriceInput.value;
        if (!limitPrice || Number(limitPrice) <= 0) {
            statusEl.textContent = "Podaj cenę LIMIT > 0";
            limitPriceInput.focus();
            return;
        }
    }

    if (orderMode === "market" && marketPrice && cachedMaxOrderValue) {
        const estValue = Number(quantity) * marketPrice;
        if (estValue >= cachedMaxOrderValue * CONFIRM_THRESHOLD_RATIO) {
            const confirmed = await confirmDialog(
                `Duże zlecenie: ${side === "buy" ? "KUP" : "SPRZEDAJ"} ${quantity} × ${ticker.split("_")[0]} ` +
                `≈ ${estValue.toFixed(2)} (limit: ${cachedMaxOrderValue.toFixed(2)}). Kontynuować?`
            );
            if (!confirmed) return;
        }
    }

    btns.forEach((b) => (b.disabled = true));
    statusEl.textContent = orderMode === "limit" ? "wysyłanie LIMIT…" : "wysyłanie…";

    try {
        const url = orderMode === "limit" ? "/warp/order/limit" : "/warp/order";
        const body = orderMode === "limit"
            ? { ticker, side, quantity, price: limitPrice }
            : { ticker, side, quantity, estimated_price: marketPrice };
        const resp = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (data.ok) {
            playSuccess();
            statusEl.textContent = orderMode === "limit" ? `LIMIT OK #${data.order_id ?? "?"} @ ${data.price}` : `OK #${data.order_id ?? "?"}`;
            const flash = document.getElementById("instrument-flash");
            flash.classList.add("focus-tile__flash--ok");
            setTimeout(() => flash.classList.remove("focus-tile__flash--ok"), 400);
            if (orderMode === "limit") {
                await loadTradeLevels(); // odswiez linie na wykresie - nowe zlecenie pojawi sie od razu
            }
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

document.getElementById("instrument-mode-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest(".tile__mode-btn");
    if (!btn) return;
    orderMode = btn.dataset.mode;
    document.querySelectorAll("#instrument-mode-toggle .tile__mode-btn").forEach((b) => {
        b.classList.toggle("tile__mode-btn--active", b === btn);
    });
    document.getElementById("instrument-limit-price").classList.toggle("instrument-detail__limit-price--hidden", orderMode !== "limit");
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
