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

async function sendOrder(tile, side) {
    const ticker = tile.dataset.ticker;
    const quantity = getQuantity(tile);
    const estimatedPrice = getEstimatedPrice(tile);
    const buttons = tile.querySelectorAll(".tile__btn");

    if (!quantity || Number(quantity) <= 0) {
        setStatus(tile, "Podaj ilość > 0");
        return;
    }

    // Modal potwierdzenia - TYLKO gdy mamy zarówno cenę ręczną, jak i
    // skonfigurowany Hard Cap (bez tego nie ma z czego liczyć progu 70%).
    if (estimatedPrice && cachedMaxOrderValue) {
        const estValue = Number(quantity) * Number(estimatedPrice);
        if (estValue >= cachedMaxOrderValue * CONFIRM_THRESHOLD_RATIO) {
            const label = side === "buy" ? "KUP" : "SPRZEDAJ";
            const confirmed = await confirmDialog(
                `Duże zlecenie: ${label} ${quantity} × ${ticker} ` +
                `≈ ${estValue.toFixed(2)} (limit: ${cachedMaxOrderValue.toFixed(2)}). Kontynuować?`
            );
            if (!confirmed) return;
        }
    }

    buttons.forEach((b) => (b.disabled = true));
    setStatus(tile, "wysyłanie…");

    try {
        const resp = await fetch("/warp/order", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ticker,
                side,
                quantity,
                estimated_price: estimatedPrice,
            }),
        });
        const data = await resp.json();

        if (data.ok) {
            flashTile(tile, "ok");
            playSuccess();
            setStatus(tile, `OK #${data.order_id ?? "?"}`);
            loadAccount();
        } else if (data.blocked) {
            flashTile(tile, "error");
            playError();
            setStatus(tile, `ZABLOKOWANE: ${data.reason ?? data.decision}`);
        } else {
            flashTile(tile, "error");
            playError();
            setStatus(tile, `BŁĄD: ${data.error ?? "nieznany"}`);
        }
    } catch (err) {
        flashTile(tile, "error");
        playError();
        setStatus(tile, "BŁĄD SIECI");
        console.error(err);
    } finally {
        buttons.forEach((b) => (b.disabled = false));
    }
}

document.querySelectorAll(".tile").forEach(setupPresets);

document.getElementById("warp-grid").addEventListener("click", (event) => {
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
        badge.textContent = "brak pozycji";
        badge.className = "tile__pnl tile__pnl--empty";
        return;
    }
    const ppl = Number(position.ppl);
    const qty = position.quantity;
    const sign = ppl >= 0 ? "+" : "";
    badge.textContent = `${qty} szt. · ${sign}${formatMoney(ppl)}`;
    badge.className = "tile__pnl " + (ppl >= 0 ? "tile__pnl--profit" : "tile__pnl--loss");
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

    setAccountStatus("ładowanie…");
    try {
        const resp = await fetch("/warp/account");
        const data = await resp.json();

        if (!data.ok) {
            setAccountStatus(`Błąd: ${data.error ?? "nieznany"}`);
            setConnDot("error");
            return;
        }

        setConnDot("ok");

        // Kształt odpowiedzi cash zależy od T212 - defensywnie próbujemy
        // kilku możliwych pól zamiast zakładać jedno konkretne.
        const cash = data.cash || {};
        const free = cash.free ?? cash.total ?? null;
        if (data.cash_error) {
            document.getElementById("account-balance").textContent =
                "saldo: niedostępne (brak uprawnień klucza API)";
        } else {
            document.getElementById("account-balance").textContent =
                free !== null ? `saldo: ${formatMoney(free)}` : "saldo: brak danych";
        }

        const positionsByTicker = {};
        (data.positions || []).forEach((p) => { positionsByTicker[p.ticker] = p; });

        document.querySelectorAll(".tile").forEach((tile) => {
            updatePnlBadge(tile, positionsByTicker[tile.dataset.ticker] || null);
        });

        setAccountStatus("");
    } catch (err) {
        setAccountStatus("Błąd sieci przy ładowaniu konta.");
        setConnDot("error");
        console.error(err);
    }
}

async function cancelAll(side) {
    const label = side === "buy" ? "KUP" : "SPRZEDAJ";
    setAccountStatus(`Anulowanie wszystkich zleceń ${label}…`);

    try {
        const resp = await fetch("/warp/cancel-all", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ side }),
        });
        const data = await resp.json();

        if (!data.ok) {
            setAccountStatus(`Błąd: ${data.error ?? "nieznany"}`);
            return;
        }
        setAccountStatus(`Anulowano ${data.cancelled}/${data.total} zleceń ${label}.`);
        loadAccount();
    } catch (err) {
        setAccountStatus("Błąd sieci przy anulowaniu.");
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
    container.innerHTML = '<p class="sidebar-widget__empty">ładowanie…</p>';

    try {
        const resp = await fetch("/warp/pending");
        const data = await resp.json();

        if (!data.ok) {
            container.innerHTML = `<p class="sidebar-widget__empty">Błąd: ${data.error ?? "nieznany"}</p>`;
            return;
        }

        const orders = data.orders || [];
        if (orders.length === 0) {
            container.innerHTML = '<p class="sidebar-widget__empty">Brak oczekujących zleceń.</p>';
            return;
        }

        container.innerHTML = "";
        orders.forEach((o) => {
            const qty = Number(o.quantity);
            const side = qty >= 0 ? "KUP" : "SPRZEDAJ";
            const div = document.createElement("div");
            div.className = "pending-orders-list__item";
            div.textContent = `${o.ticker ?? "?"} · ${side} ${Math.abs(qty)}`;
            container.appendChild(div);
        });
    } catch (err) {
        container.innerHTML = '<p class="sidebar-widget__empty">Błąd sieci.</p>';
        console.error(err);
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
                statusEl.textContent = "Nie udało się zmienić siatki.";
                btn.disabled = false;
            }
        } catch (err) {
            statusEl.textContent = "Błąd sieci.";
            btn.disabled = false;
            console.error(err);
        }
    });
});
