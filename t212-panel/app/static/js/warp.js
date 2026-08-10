/*
warp.js
=======
Obsługa siatki 3x3. playTone/playSuccess/playError PRZENIESIONE do common.js
(ładowane na każdej stronie) - pie.js (Smart Virtual Pie) też ich potrzebuje,
więc trzymanie ich tylko tutaj oznaczałoby duplikację.
*/

function flashTile(tile, kind) {
    const flash = tile.querySelector(".tile__flash");
    flash.classList.remove("tile__flash--ok", "tile__flash--error");
    // wymuszenie reflow, żeby animacja zadziałała przy powtórnym kliknięciu
    void flash.offsetWidth;
    flash.classList.add(kind === "ok" ? "tile__flash--ok" : "tile__flash--error");
    setTimeout(() => flash.classList.remove("tile__flash--ok", "tile__flash--error"), 280);
}

function setStatus(tile, text) {
    tile.querySelector(".tile__status").textContent = text;
}

/*
Presety ilości: klik na 0.1 / 0.5 / 1 ustawia aktywny preset (podświetlenie
+ ukrycie pola własnej ilości). Klik na "wł." pokazuje pole do wpisania
własnej wartości i robi na nim focus - jeden dodatkowy tap+wpisanie zamiast
domyślnego stanu, świadomy kompromis żeby najczęstsze rozmiary (0.1/0.5/1)
były NAPRAWDĘ jednoklikowe.
*/
function setupPresets(tile) {
    const presetButtons = tile.querySelectorAll(".tile__preset");
    const customInput = tile.querySelector(".tile__qty-custom-input");

    presetButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
            presetButtons.forEach((b) => b.classList.remove("tile__preset--active"));
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

function getQuantity(tile) {
    const activePreset = tile.querySelector(".tile__preset--active");
    if (activePreset && activePreset.dataset.qty !== "custom") {
        return activePreset.dataset.qty;
    }
    const customInput = tile.querySelector(".tile__qty-custom-input");
    return customInput.value;
}

function getEstimatedPrice(tile) {
    const priceInput = tile.querySelector(".tile__price");
    return priceInput.value || null; // puste pole -> null, risk_guard przechodzi w tryb post-check
}

/*
=== Cena live z Finnhub (auto-wypelniana, ale nadpisywalna recznie) ===
tile__price bylo czysto reczne pole (T212 nie zwraca ceny w API zlecen) -
teraz domyslnie wypelniane z /warp/quote (ten sam endpoint co Focus Mode).
Jesli user sam wpisze wartosc, przestajemy nadpisywac TO KONKRETNE pole
(dataset.autoFilled) - reczna kontrola ma pierwszenstwo, live-cena to tylko
wygodny domyslny start.
*/
document.querySelectorAll(".tile__price").forEach((input) => {
    input.dataset.autoFilled = "true";
    input.addEventListener("input", () => { input.dataset.autoFilled = "false"; });
});

async function refreshTilePrices() {
    for (const tile of document.querySelectorAll(".tile")) {
        const priceInput = tile.querySelector(".tile__price");
        if (priceInput.dataset.autoFilled === "false") continue; // user nadpisal recznie - nie ruszamy

        const ticker = tile.dataset.ticker;
        try {
            const resp = await fetch(`/warp/quote?ticker=${encodeURIComponent(ticker)}`);
            const data = await resp.json();
            if (data.ok) {
                priceInput.value = Number(data.quote.c).toFixed(2);
                priceInput.dataset.autoFilled = "true"; // .value= nie odpala 'input', flaga zostaje true
            }
        } catch (err) {
            console.error("Cena live:", ticker, err);
        }
    }
}

// Interwal skalowany liczba kafelkow (ten sam pomysl co Focus Mode) - zeby
// nie przekroczyc 60 req/min limitu Finnhub nawet przy pelnej siatce 9x9.
const priceRefreshMs = Math.max(8000, document.querySelectorAll(".tile").length * 1200);
refreshTilePrices();
setInterval(refreshTilePrices, priceRefreshMs);

let cachedMaxOrderValue = null; // null = brak skonfigurowanego Hard Cap (jeszcze)
const CONFIRM_THRESHOLD_RATIO = 0.7; // pytamy o potwierdzenie od 70% limitu wzwyż

async function loadLimits() {
    try {
        const resp = await fetch("/warp/limits");
        const data = await resp.json();
        cachedMaxOrderValue = data.max_order_value ? Number(data.max_order_value) : null;
    } catch (err) {
        console.error("Nie udało się pobrać limitów:", err);
    }
}

// Tryb zlecenia (RYNEK/LIMIT) - przelacznik per-kafelek (Adam, 2026-07-31:
// "zrob po prostu switch market/limit a guziki zostaw te same" - poprzednia
// wersja z osobnym drugim rzedem "LIMIT KUP"/"LIMIT SPRZEDAJ" psula layout
// (kafelek za niski, przyciski wystawaly poza ramke, patrz ss3/ss4.png).
// Teraz TE SAME przyciski KUP/SPRZEDAJ wysylaja MARKET albo LIMIT zaleznie
// od aktywnego trybu - zero dodatkowych przyciskow, zero dodatkowej
// wysokosci kafelka. tile.dataset.orderMode - "market" (domyslnie) albo
// "limit", ustawiane klikiem w .tile__mode-btn.
function getOrderMode(tile) {
    return tile.dataset.orderMode || "market";
}

async function sendOrder(tile, side) {
    const mode = getOrderMode(tile);
    const ticker = tile.dataset.ticker;
    const quantity = getQuantity(tile);
    const price = getEstimatedPrice(tile);
    const buttons = tile.querySelectorAll(".tile__btn");

    if (!quantity || Number(quantity) <= 0) {
        setStatus(tile, t("Podaj ilość > 0"));
        return;
    }
    if (mode === "limit" && (!price || Number(price) <= 0)) {
        setStatus(tile, t("Podaj cenę LIMIT > 0 (pole ceny powyżej)"));
        return;
    }
    if (mode === "stop" && (!price || Number(price) <= 0)) {
        setStatus(tile, t("Podaj cenę STOP > 0 (pole ceny powyżej)"));
        return;
    }
    const stopLimitPriceInput = tile.querySelector(".tile__stoplimit-price");
    const stopLimitPrice = stopLimitPriceInput ? stopLimitPriceInput.value : null;
    if (mode === "stoplimit" && ((!price || Number(price) <= 0) || (!stopLimitPrice || Number(stopLimitPrice) <= 0))) {
        setStatus(tile, t("Podaj cenę STOP (pole u góry) i cenę LIMIT (pole niżej), obie > 0"));
        return;
    }

    // Modal potwierdzenia - TYLKO w trybie RYNEK, gdy mamy zarówno cenę
    // ręczną, jak i skonfigurowany Hard Cap (bez tego nie ma z czego liczyć
    // progu 70%). W trybie LIMIT cena jest już wymagana/celowa, nie
    // szacunkowa - risk_guard po stronie serwera i tak sprawdza Hard Cap.
    if (mode === "market" && price && cachedMaxOrderValue) {
        const estValue = Number(quantity) * Number(price);
        if (estValue >= cachedMaxOrderValue * CONFIRM_THRESHOLD_RATIO) {
            const label = side === "buy" ? t("KUP") : t("SPRZEDAJ");
            const confirmed = await confirmDialog(
                `${t("Duże zlecenie")}: ${label} ${quantity} × ${ticker} ` +
                `≈ ${estValue.toFixed(2)} (limit: ${cachedMaxOrderValue.toFixed(2)}). ${t("Kontynuować?")}`
            );
            if (!confirmed) return;
        }
    }

    buttons.forEach((b) => (b.disabled = true));
    setStatus(
        tile,
        mode === "limit" ? `${t("wysyłanie")} LIMIT…`
        : mode === "stop" ? `${t("wysyłanie")} STOP…`
        : mode === "stoplimit" ? `${t("wysyłanie")} STOP-LIMIT…`
        : `${t("wysyłanie")}…`,
    );

    try {
        const url = mode === "limit" ? "/warp/order/limit"
            : mode === "stop" ? "/warp/order/stop"
            : mode === "stoplimit" ? "/warp/order/stop-limit"
            : "/warp/order";
        const body = mode === "limit"
            ? { ticker, side, quantity, price }
            : mode === "stop"
            ? { ticker, side, quantity, stop_price: price }
            : mode === "stoplimit"
            ? { ticker, side, quantity, stop_price: price, limit_price: stopLimitPrice }
            : { ticker, side, quantity, estimated_price: price };
        const resp = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (data.ok) {
            flashTile(tile, "ok");
            playSuccess();
            setStatus(
                tile,
                mode === "limit" ? `LIMIT OK #${data.order_id ?? "?"} @ ${data.price}`
                : mode === "stop" ? `STOP OK #${data.order_id ?? "?"} @ ${data.stop_price}`
                : mode === "stoplimit" ? `STOP-LIMIT OK #${data.order_id ?? "?"} @ ${data.stop_price}/${data.limit_price}`
                : `OK #${data.order_id ?? "?"}`,
            );
            if (mode === "limit" || mode === "stop" || mode === "stoplimit") {
                loadPendingOrders();
            } else {
                loadAccount();
            }
        } else if (data.blocked) {
            flashTile(tile, "error");
            playError();
            setStatus(tile, `${t("ZABLOKOWANE")}: ${t(data.reason) ?? t(data.decision)}`);
        } else {
            flashTile(tile, "error");
            playError();
            setStatus(tile, `${t("BŁĄD")}: ${t(data.error) ?? t("nieznany")}`);
        }
    } catch (err) {
        flashTile(tile, "error");
        playError();
        setStatus(tile, t("BŁĄD SIECI"));
        console.error(err);
    } finally {
        buttons.forEach((b) => (b.disabled = false));
    }
}

document.querySelectorAll(".tile").forEach(setupPresets);

document.getElementById("warp-grid").addEventListener("click", (event) => {
    const modeBtn = event.target.closest(".tile__mode-btn");
    if (modeBtn) {
        const tile = modeBtn.closest(".tile");
        tile.dataset.orderMode = modeBtn.dataset.mode;
        tile.querySelectorAll(".tile__mode-btn").forEach((b) => {
            b.classList.toggle("tile__mode-btn--active", b === modeBtn);
        });
        const stopLimitInput = tile.querySelector(".tile__stoplimit-price");
        if (stopLimitInput) {
            stopLimitInput.classList.toggle("tile__stoplimit-price--hidden", modeBtn.dataset.mode !== "stoplimit");
        }
        return;
    }
    const btn = event.target.closest(".tile__btn");
    if (!btn) return;
    const tile = btn.closest(".tile");
    sendOrder(tile, btn.dataset.side);
});

/*
=== Saldo, P&L per pozycja, anulowanie zbiorcze ===
*/

function setAccountStatus(text) {
    document.getElementById("account-status").textContent = text;
}

function formatMoney(value) {
    const num = Number(value);
    if (Number.isNaN(num)) return "—";
    return num.toFixed(2);
}

function updatePnlBadge(tile, position) {
    const badge = tile.querySelector(".tile__pnl");
    if (!position) {
        // "brak pozycji" NIE jest wartością pieniężną - bez klasy js-money,
        // żeby hide/show (patrz common.js) jej nie dotykał.
        delete badge.dataset.real;
        badge.textContent = t("brak pozycji");
        badge.className = "tile__pnl tile__pnl--empty";
        return;
    }
    const ppl = Number(position.ppl);
    const qty = position.quantity;
    const sign = ppl >= 0 ? "+" : "";
    badge.dataset.real = `${qty} ${t("szt.")} · ${sign}${formatMoney(ppl)}`;
    badge.className = "tile__pnl js-money " + (ppl >= 0 ? "tile__pnl--profit" : "tile__pnl--loss");
}

// Throttle dla loadAccount() - odkryliśmy (11.07.2026, log z NAS-a), że rate
// limit T212 jest DUŻO węższy niż zakładaliśmy: nie tylko get_instruments(),
// ale też /equity/portfolio i /equity/orders pokazują "0/1 pozostało" niemal
// od razu. Bez tego throttle'a auto-refresh (co 20s) + odświeżenie po każdym
// zleceniu + po każdym anulowaniu razem zjadały limit i psuły appkę (502,
// "Błąd sieci"). MIN_LOAD_GAP_MS wymusza minimalny odstęp między FAKTYCZNYMI
// zapytaniami - wywołania w międzyczasie są po cichu pomijane.
let lastLoadAccountAt = 0;
const MIN_LOAD_GAP_MS = 15000;

async function loadAccount(force = false) {
    const now = Date.now();
    if (!force && now - lastLoadAccountAt < MIN_LOAD_GAP_MS) {
        return; // zbyt wcześnie od ostatniego załadowania - pomijamy w ciszy
    }
    lastLoadAccountAt = now;

    setAccountStatus(t("ładowanie…"));
    try {
        const resp = await fetch("/warp/account");
        const data = await resp.json();

        if (!data.ok) {
            setAccountStatus(`${t("Błąd")}: ${t(data.error) ?? t("nieznany")}`);
            setConnDot("error");
            return;
        }

        setConnDot("ok");

        // Kształt odpowiedzi cash zależy od T212 - defensywnie próbujemy
        // kilku możliwych pól zamiast zakładać jedno konkretne.
        const cash = data.cash || {};
        const free = cash.free ?? cash.total ?? null;
        const balanceEl = document.getElementById("account-balance");
        if (data.cash_error) {
            // Komunikat o błędzie NIE jest wartością pieniężną - zdejmij js-money
            // (patrz updatePnlBadge, identyczny powód), inaczej hide/show
            // podmieniłby go na kropki zamiast realnego opisu błędu.
            balanceEl.classList.remove("js-money");
            delete balanceEl.dataset.real;
            balanceEl.textContent = t("saldo: niedostępne (brak uprawnień klucza API)");
        } else if (free !== null) {
            balanceEl.classList.add("js-money");
            balanceEl.dataset.real = `${t("saldo")}: ${formatMoney(free)}`;
        } else {
            balanceEl.classList.remove("js-money");
            delete balanceEl.dataset.real;
            balanceEl.textContent = t("saldo: brak danych");
        }

        const positionsByTicker = {};
        (data.positions || []).forEach((p) => { positionsByTicker[p.ticker] = p; });

        document.querySelectorAll(".tile").forEach((tile) => {
            updatePnlBadge(tile, positionsByTicker[tile.dataset.ticker] || null);
        });
        applyMoneyHiding();  // patrz common.js - saldo/badge'e wyżej mają świeże dataset.real

        setAccountStatus("");
    } catch (err) {
        setAccountStatus(t("Błąd sieci przy ładowaniu konta."));
        setConnDot("error");
        console.error(err);
    }
}

async function cancelAll(side) {
    const label = side === "buy" ? t("KUP") : t("SPRZEDAJ");
    setAccountStatus(`${t("Anulowanie wszystkich zleceń")} ${label}…`);

    try {
        const resp = await fetch("/warp/cancel-all", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ side }),
        });
        const data = await resp.json();

        if (!data.ok) {
            setAccountStatus(`${t("Błąd")}: ${t(data.error) ?? t("nieznany")}`);
            return;
        }
        setAccountStatus(`${t("Anulowano")} ${data.cancelled}/${data.total} ${t("zleceń")} ${label}.`);
        loadAccount();
    } catch (err) {
        setAccountStatus(t("Błąd sieci przy anulowaniu."));
        console.error(err);
    }
}

document.getElementById("btn-refresh").addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    await loadAccount(true);
    // Cooldown 10s - bez tego łatwo przypadkiem kliknąć drugi raz zaraz po
    // pierwszym i dostać 429 (too many requests) od T212, którego rate
    // limit jest bardzo wąski (patrz komentarz przy MIN_LOAD_GAP_MS wyżej).
    setTimeout(() => { btn.disabled = false; }, 10000);
});
document.getElementById("btn-cancel-buy").addEventListener("click", () => cancelAll("buy"));
document.getElementById("btn-cancel-sell").addEventListener("click", () => cancelAll("sell"));

/*
=== Panel "Otwarte zlecenia" (prawy sidebar) ===
Ładowane automatycznie przy wejściu na stronę (na życzenie Adama, 18.07.2026 -
wcześniej wymagało kliknięcia "Pokaż", co było mylące skoro widget i tak jest
zawsze widoczny). To trzeci automatyczny request do wąskiego rate limitu T212
obok loadAccount()/loadLimits() - stąd 1.5s opóźnienia poniżej, żeby nie
strzelały wszystkie trzy jednym batchem w tej samej milisekundzie.
*/
async function loadPendingOrders() {
    const container = document.getElementById("pending-orders-list");
    container.innerHTML = `<p class="sidebar-widget__empty">${t("ładowanie…")}</p>`;

    try {
        const resp = await fetch("/warp/pending");
        const data = await resp.json();

        if (!data.ok) {
            container.innerHTML = `<p class="sidebar-widget__empty">${t("Błąd")}: ${t(data.error) ?? t("nieznany")}</p>`;
            return;
        }

        const orders = data.orders || [];
        if (orders.length === 0) {
            container.innerHTML = `<p class="sidebar-widget__empty">${t("Brak oczekujących zleceń.")}</p>`;
            return;
        }

        container.innerHTML = "";
        orders.forEach((o) => {
            const qty = Number(o.quantity);
            const side = qty >= 0 ? t("KUP") : t("SPRZEDAJ");
            const div = document.createElement("div");
            div.className = "pending-orders-list__item";

            const nameEl = document.createElement("span");
            nameEl.className = "pending-orders-list__name";
            nameEl.textContent = o.name || o.ticker || "?";
            div.appendChild(nameEl);

            const metaEl = document.createElement("span");
            metaEl.className = "pending-orders-list__meta";
            metaEl.textContent = `${o.ticker ?? "?"} · ${side} ${Math.abs(qty)}`;
            div.appendChild(metaEl);

            // Edycja/kasowanie - dodane 2026-07-30 (Adam: "chce zeby mozna
            // bylo edytowac cyfrowo podciagajac badz obnizajac cene").
            // TYLKO dla o.editable=true (LIMIT BUY nie sledzone przez bota,
            // patrz scalping.py::_bot_order_sources) - zlecenia bota
            // zostaja jak wyzej, bez zadnych przyciskow.
            if (o.editable && o.limitPrice != null) {
                buildEditableOrderRow(div, o);
            }

            container.appendChild(div);
        });
    } catch (err) {
        container.innerHTML = `<p class="sidebar-widget__empty">${t("Błąd sieci.")}</p>`;
        console.error(err);
    }
}

// Dopisuje wiersz cena + przyciski +/- + Zatwierdz/Anuluj (po zmianie) +
// Skasuj do istniejacego elementu diva pojedynczego zlecenia. +/- tylko
// PRZESUWAJA lokalny "staged" stan (podglad) - realne anuluj+zloz-nowe
// wychodzi do T212 dopiero po kliknieciu Zatwierdz (ten sam wzorzec
// potwierdzenia co przy przeciaganiu linii na wykresie, instrument.js).
function buildEditableOrderRow(div, order) {
    const orderId = String(order.id);
    const originalPrice = Number(order.limitPrice);
    const originalQuantity = Number(order.quantity);
    let stagedPrice = originalPrice;
    let stagedQuantity = originalQuantity;
    const priceStep = originalPrice * 0.001 || 0.01; // ~0,1% ceny na klikniecie
    // Ilosc: krok procentowy (10%) zamiast stalej liczby - zleceni bywaja
    // ulamkowe (0.1, 1.813 itd.), stala wartosc byłaby albo za duza dla
    // malych, albo za mala dla duzych. Dodane 2026-07-30 (Adam: "zmiany
    // ilosci nie zaimplementowales a powinna byc").
    const qtyStep = originalQuantity * 0.1 || 0.01;

    // Kazde pole (cena/ilosc) w WLASNYM wierszu razem ze SWOIMI przyciskami
    // +/- (nie wszystkie 4 nudge + akcje w jednym rzedzie) - naprawione
    // 2026-07-30 (Adam: "panel sie rozjezdza jak klikam na przyciski"),
    // wczesniej wszystkie przyciski lecialy w jeden, zbyt waski wiersz
    // sidebaru i zawijaly sie w nieprzewidywalny sposob.
    function fieldRow(valueClassName) {
        const row = document.createElement("div");
        row.className = "pending-orders-list__field-row";
        const valueEl = document.createElement("span");
        valueEl.className = valueClassName;
        row.appendChild(valueEl);
        div.appendChild(row);
        return { row, valueEl };
    }

    function nudgeBtn(row, label) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pending-orders-list__nudge-btn";
        btn.textContent = label;
        row.appendChild(btn);
        return btn;
    }

    const { valueEl: priceEl, row: priceRow } = fieldRow("pending-orders-list__price");
    priceEl.textContent = originalPrice.toFixed(4);
    const priceMinusBtn = nudgeBtn(priceRow, "−");
    const pricePlusBtn = nudgeBtn(priceRow, "+");

    const { valueEl: qtyEl, row: qtyRow } = fieldRow("pending-orders-list__price");
    qtyEl.textContent = `${originalQuantity} ${t("szt.")}`;
    const qtyMinusBtn = nudgeBtn(qtyRow, "−");
    const qtyPlusBtn = nudgeBtn(qtyRow, "+");

    const controlsEl = document.createElement("div");
    controlsEl.className = "pending-orders-list__controls";
    div.appendChild(controlsEl);

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "pending-orders-list__action-btn pending-orders-list__action-btn--cancel";
    cancelBtn.textContent = t("Skasuj");
    controlsEl.appendChild(cancelBtn);

    let confirmBtn = null;
    let revertBtn = null;

    function isStaged() {
        return Math.abs(stagedPrice - originalPrice) > 1e-9 || Math.abs(stagedQuantity - originalQuantity) > 1e-9;
    }

    function renderStaged() {
        // Pokazujemy TYLKO jedną, aktualną liczbę (nie "stara -> nowa") -
        // naprawione 2026-07-30 (Adam: "cena/ilość ma się po prostu zmieniać,
        // a nie tańczyć po ekranie") - dwuliczbowy zapis zmieniał szerokość
        // wiersza przy każdym kliknięciu, przesuwając przyciski. Niezapisana
        // zmiana sygnalizowana samym kolorem (klasa --staged na wartości),
        // bez zmiany długości tekstu ani układu.
        const priceChanged = Math.abs(stagedPrice - originalPrice) > 1e-9;
        priceEl.textContent = stagedPrice.toFixed(4);
        priceEl.classList.toggle("pending-orders-list__price--staged", priceChanged);

        const qtyChanged = Math.abs(stagedQuantity - originalQuantity) > 1e-9;
        qtyEl.textContent = `${stagedQuantity.toFixed(4)} ${t("szt.")}`;
        qtyEl.classList.toggle("pending-orders-list__price--staged", qtyChanged);

        if (!isStaged()) {
            if (confirmBtn) { confirmBtn.remove(); confirmBtn = null; }
            if (revertBtn) { revertBtn.remove(); revertBtn = null; }
            return;
        }

        if (!confirmBtn) {
            confirmBtn = document.createElement("button");
            confirmBtn.type = "button";
            confirmBtn.className = "pending-orders-list__action-btn pending-orders-list__action-btn--confirm";
            confirmBtn.textContent = t("Zatwierdź");
            confirmBtn.addEventListener("click", async () => {
                div.querySelectorAll("button").forEach((b) => { b.disabled = true; });
                const ok = await sendReprice(orderId, stagedPrice, stagedQuantity);
                if (ok) {
                    loadPendingOrders(); // odswiez cala liste - nowy order_id po anuluj+zloz-nowe
                } else {
                    div.querySelectorAll("button").forEach((b) => { b.disabled = false; });
                }
            });
            controlsEl.appendChild(confirmBtn);
        }
        if (!revertBtn) {
            revertBtn = document.createElement("button");
            revertBtn.type = "button";
            revertBtn.className = "pending-orders-list__action-btn";
            revertBtn.textContent = t("Anuluj");
            revertBtn.addEventListener("click", () => {
                stagedPrice = originalPrice;
                stagedQuantity = originalQuantity;
                renderStaged();
            });
            controlsEl.appendChild(revertBtn);
        }
    }

    priceMinusBtn.addEventListener("click", () => {
        stagedPrice = Math.max(0.0001, stagedPrice - priceStep);
        renderStaged();
    });
    pricePlusBtn.addEventListener("click", () => {
        stagedPrice += priceStep;
        renderStaged();
    });
    qtyMinusBtn.addEventListener("click", () => {
        stagedQuantity = Math.max(0.0001, stagedQuantity - qtyStep);
        renderStaged();
    });
    qtyPlusBtn.addEventListener("click", () => {
        stagedQuantity += qtyStep;
        renderStaged();
    });
    // Skasuj = JEDEN klik, bez potwierdzenia (Adam, 2026-07-30: "kasuje i
    // nic się nie dzieje kumasz?" - najpierw natywny confirm() mylil sie
    // z przyciskiem "Anuluj" obok, potem podwojne-kliknicie-do-potwierdzenia
    // bylo kolejnym zrodlem "nic sie nie dzieje" - user nie zauwazal zmiany
    // tekstu po pierwszym kliknieciu. Niskie ryzyko pomylki - w najgorszym
    // razie trzeba zlozyc zlecenie ponownie).
    cancelBtn.addEventListener("click", async () => {
        cancelBtn.disabled = true;
        const ok = await sendCancelOrder(orderId);
        if (ok) {
            loadPendingOrders();
        } else {
            cancelBtn.disabled = false;
        }
    });
}

async function sendReprice(orderId, newPrice, newQuantity) {
    try {
        const resp = await fetch(`/warp/order/${encodeURIComponent(orderId)}/reprice`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ new_price: newPrice, new_quantity: newQuantity }),
        });
        const data = await resp.json();
        if (!data.ok) {
            alert(t(data.error) || t(data.reason) || t("Nie udało się zmienić zlecenia."));
            return false;
        }
        return true;
    } catch (err) {
        console.error("Błąd repricingu zlecenia:", err);
        alert(t("Błąd połączenia przy zmianie ceny zlecenia."));
        return false;
    }
}

async function sendCancelOrder(orderId) {
    try {
        const resp = await fetch(`/warp/order/${encodeURIComponent(orderId)}/cancel`, { method: "POST" });
        const data = await resp.json();
        if (!data.ok) {
            alert(t(data.error) || t("Nie udało się skasować zlecenia."));
            return false;
        }
        return true;
    } catch (err) {
        console.error("Błąd kasowania zlecenia:", err);
        alert(t("Błąd połączenia przy kasowaniu zlecenia."));
        return false;
    }
}

// Ładowanie od razu przy wejściu na stronę + auto-odświeżanie co 90s.
// UWAGA: było 20s, ale rate limit T212 na /equity/portfolio jest znacznie
// węższy niż zakładaliśmy (patrz komentarz przy MIN_LOAD_GAP_MS wyżej) -
// nie skracaj tego bez ponownego sprawdzenia w logu serwera, ile zapytań
// T212 faktycznie toleruje.
loadAccount();
loadLimits();
setTimeout(loadPendingOrders, 1500);
setInterval(loadAccount, 90000);

/*
=== Klikalne "Ulubione" w sidebarze - toggle obecności w siatce 3x3 ===
Klik na ticker NIE będący w siatce -> dodaje (POST /settings/grid/add).
Klik na ticker JUŻ w siatce -> usuwa (POST /settings/grid/remove).
Po sukcesie: przeładowanie strony - środkowa siatka jest renderowana po
stronie serwera z tickers, więc to najprostszy sposób żeby na pewno
pokazała aktualny stan (bez duplikowania logiki renderowania kafelków w JS).
*/
document.querySelectorAll(".sidebar-favorites__toggle").forEach((btn) => {
    btn.addEventListener("click", async () => {
        const ticker = btn.dataset.ticker;
        const inGrid = btn.dataset.inGrid === "true";
        const endpoint = inGrid ? "/settings/grid/remove" : "/settings/grid/add";
        const statusEl = document.getElementById("grid-toggle-status");

        btn.disabled = true;
        statusEl.textContent = "…";

        try {
            const formData = new URLSearchParams();
            formData.set("ticker", ticker);
            const resp = await fetch(endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/x-www-form-urlencoded" },
                body: formData.toString(),
            });
            // Endpointy /settings/grid/* przekierowują (302) z powrotem do
            // /warp/ po sukcesie - fetch podąża za przekierowaniem domyślnie,
            // więc resp.ok=true oznacza że dotarliśmy z powrotem poprawnie.
            if (resp.ok) {
                window.location.reload();
            } else {
                statusEl.textContent = t("Nie udało się zmienić siatki.");
                btn.disabled = false;
            }
        } catch (err) {
            statusEl.textContent = t("Błąd sieci.");
            btn.disabled = false;
            console.error(err);
        }
    });
});
