/*
common.js
=========
Wspólne narzędzia UI ładowane na KAŻDEJ stronie (przez base.html):
- confirmDialog(message) - modal potwierdzenia zamiast brzydkiego window.confirm()
- setConnDot(status) - aktualizacja kropki statusu połączenia z T212 w topbarze
- playSuccess()/playError() - dźwięki (Web Audio, bez plików mp3)

Trzymane osobno od warp.js, bo modal i kropka żyją w base.html (a więc
dotyczą też stron poza Warp Mode - np. Smart Virtual Pie w pie.js, które
reużywa też playSuccess/playError zamiast duplikować logikę dźwięku).
*/

/*
t(text) - JS odpowiednik app/i18n.py::t(). window.SNAJPER_LANG i
window.SNAJPER_I18N wstrzykiwane są w base.html (inline <script> przed
tym plikiem) z tego samego kontekstu Jinja co current_lang/i18n_json, więc
oba t() (Python i JS) czytają DOKŁADNIE ten sam słownik en.txt - żaden
tekst dorenderowany przez JS (tooltipy, statusy, alerty) nie zostaje po
polsku gdy user przełączy się na EN.
*/
function t(text) {
    if (window.SNAJPER_LANG !== "en") return text;
    return (window.SNAJPER_I18N && window.SNAJPER_I18N[text]) || text;
}

/*
avatarHue(ticker) - JS odpowiednik utils.py::avatar_hue(), ten sam wzór
(suma kodów znaków % 360), żeby kolor awatara fallback dla tego samego
tickera ZAWSZE wychodził identyczny czy liczony po stronie serwera
(Jinja, przy renderze strony) czy tutaj (JS, przy dorenderowaniu wyników
wyszukiwania bez przeładowania strony).
*/
function avatarHue(ticker) {
    let sum = 0;
    for (let i = 0; i < ticker.length; i++) sum += ticker.charCodeAt(i);
    return sum % 360;
}

/*
Dźwięki - bez zewnętrznych plików audio (error.mp3/success.mp3 z pierwotnego
pomysłu Gemini), generowane na żywo przez Web Audio API (prosty oscylator),
żeby appka nie zależała od dodatkowych plików binarnych do wgrania na NAS.
Przeniesione tu z warp.js, bo pie.js (Smart Virtual Pie) też ich potrzebuje.
*/
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

// Glosnosc powiadomien dzwiekowych - 0-100%, trzymana w localStorage (czysto
// kosmetyczna preferencja przegladarki, nie ma potrzeby synchronizowac jej
// przez serwer jak np. motywu jasny/ciemny). MAX_SOUND_GAIN to faktyczny
// gain Web Audio przy 100% - podbijanie samego suwaka wyzej niz to
// zaczyna przeszkadzac (zniekształcenia przy prostych falach square/sine).
const SOUND_VOLUME_KEY = "sniper_sound_volume_pct";
const MAX_SOUND_GAIN = 0.35;

function getSoundVolumePct() {
    // UWAGA: localStorage.getItem() zwraca null gdy klucz nigdy nie byl
    // ustawiony (czyli dla KAZDEGO usera dopoki nie ruszy suwaka), a
    // Number(null) w JS to 0, NIE NaN - bez tego jawnego sprawdzenia
    // brak zapisanej preferencji wychodzil jako "wyciszone" zamiast
    // domyslnych 100%. To byl realny bug: dzwiek byl cichy dla WSZYSTKICH,
    // zawsze, odkad powstal suwak.
    const raw = localStorage.getItem(SOUND_VOLUME_KEY);
    if (raw === null) return 100;
    const stored = Number(raw);
    if (!Number.isFinite(stored)) return 100;
    return Math.min(100, Math.max(0, stored));
}

function setSoundVolumePct(pct) {
    localStorage.setItem(SOUND_VOLUME_KEY, String(Math.min(100, Math.max(0, pct))));
}

function playTone(freq, durationMs, waveType) {
    // AudioContext startuje w stanie "suspended" dopoki przegladarka nie
    // zobaczy gestu usera (autoplay policy) - powstal PRZED pierwszym
    // klikniecim (linijka wyzej, przy zaladowaniu strony), wiec bez tego
    // resume() dzwiek nigdy sie nie odblokowuje, mimo ze playTone() i tak
    // jest wolane wylacznie z handlerow klikniec (Kup/Sprzedaj itp.).
    if (audioCtx.state === "suspended") {
        audioCtx.resume();
    }
    const volumePct = getSoundVolumePct();
    if (volumePct <= 0) return; // 0% = wyciszone, nie ma sensu nawet tworzyc oscylatora

    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = freq;
    osc.type = waveType || "square";
    gain.gain.setValueAtTime(MAX_SOUND_GAIN * (volumePct / 100), audioCtx.currentTime);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + durationMs / 1000);
}

function playSuccess() { playTone(880, 90); }
function playError() { playTone(160, 180); }

/*
Dzwieki wywolywane z TIMERA (np. portfolio.js::playUpdate po 1.5s auto-
odswiezeniu, bez zadnego kliknieca) trafiaja czesto w moment gdy AudioContext
jest jeszcze "suspended" (autoplay policy przegladarek: audio odblokowuje sie
WYLACZNIE w ramach prawdziwego gestu usera - klik/klawisz/dotyk, nigdy w
callbacku setTimeout, nawet jesli ten timer sam w sobie zostal ustawiony
przez cos co user kiedys kliknal). Zeby taki dzwiek nie ginal bezpowrotnie
gdy user nic jeszcze nie kliknal - kolejkujemy go i odtwarzamy PRZY
NAJBLIZSZYM realnym gescie, zamiast po prostu cicho nic nie robic.
*/
let _pendingChime = null;

function playToneMaybeQueued(freq, durationMs, waveType) {
    if (audioCtx.state === "suspended") {
        _pendingChime = () => playTone(freq, durationMs, waveType);
        return;
    }
    playTone(freq, durationMs, waveType);
}

["click", "keydown", "touchstart"].forEach((evt) => {
    document.addEventListener(evt, () => {
        if (audioCtx.state !== "suspended") return;
        audioCtx.resume().then(() => {
            if (_pendingChime) {
                _pendingChime();
                _pendingChime = null;
            }
        });
    }, { once: true });
});

// Cichy sygnal "dane odswiezone w tle" (np. portfolio.js) - celowo INNY niz
// wynik zlecenia (playSuccess/playError), zeby nie mylic "kupno/sprzedaz OK"
// z "wlasnie doszly swiezsze dane". Fala sinusoidalna zamiast square - inna
// barwa, nie tylko wysokosc, latwiej odroznic na sluch. Uzywa kolejkowanej
// wersji (patrz wyzej), bo w praktyce ZAWSZE wola sie z timera, nie klikniecia.
function playUpdate() { playToneMaybeQueued(523, 70, "sine"); }

function confirmDialog(message) {
    return new Promise((resolve) => {
        const overlay = document.getElementById("confirm-modal");
        const messageEl = document.getElementById("confirm-modal-message");
        const yesBtn = document.getElementById("confirm-modal-yes");
        const noBtn = document.getElementById("confirm-modal-no");

        messageEl.textContent = message;
        overlay.classList.remove("modal-overlay--hidden");

        // Klonowanie przycisków żeby pozbyć się poprzednich listenerów -
        // prostsze niż ręczne removeEventListener przy powtórnym wywołaniu.
        const newYes = yesBtn.cloneNode(true);
        const newNo = noBtn.cloneNode(true);
        yesBtn.replaceWith(newYes);
        noBtn.replaceWith(newNo);

        function close(result) {
            overlay.classList.add("modal-overlay--hidden");
            resolve(result);
        }

        newYes.addEventListener("click", () => close(true));
        newNo.addEventListener("click", () => close(false));
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) close(false);
        }, { once: true });
    });
}

/*
Komunikaty flash (patrz base.html) znikaja same po kilku sekundach zamiast
czekac w nieskonczonosc na kolejna akcje/przeladowanie strony.
*/
document.querySelectorAll(".flash-bar__msg").forEach((msg) => {
    setTimeout(() => {
        msg.classList.add("flash-bar__msg--fade");
        setTimeout(() => msg.remove(), 400);
    }, 4500);
});

/*
=== Przechwytywanie błędów JS (do automatycznego dołączania w zgłoszeniach) ===
Prosty ring buffer - trzyma ostatnie 10 błędów z tej sesji przeglądarki
(nie zapisywane nigdzie trwale, znikają przy odświeżeniu strony - to
wystarczy, bo user zwykle zgłasza problem od razu po tym jak się wydarzył).
*/
const _errorLog = [];
const MAX_ERROR_LOG = 10;

function _recordError(message) {
    const timestamp = new Date().toISOString().split("T")[1].split(".")[0];
    _errorLog.push(`[${timestamp}] ${message}`);
    if (_errorLog.length > MAX_ERROR_LOG) _errorLog.shift();
}

window.addEventListener("error", (event) => {
    _recordError(`${event.message} (${event.filename}:${event.lineno})`);
});
window.addEventListener("unhandledrejection", (event) => {
    _recordError(`Unhandled promise rejection: ${event.reason}`);
});

function collectErrorLog() {
    return _errorLog.join("\n");
}

/*
=== Modal "Zgłoś problem" ===
*/
document.addEventListener("DOMContentLoaded", () => {
    const reportBtn = document.getElementById("report-problem-btn");
    const reportModal = document.getElementById("report-modal");
    if (!reportBtn || !reportModal) return; // niezalogowany - modal nie istnieje w DOM

    const form = document.getElementById("report-form");
    const statusEl = document.getElementById("report-form-status");
    const cancelBtn = document.getElementById("report-modal-cancel");

    function closeReportModal() {
        reportModal.classList.add("modal-overlay--hidden");
        statusEl.textContent = "";
    }

    reportBtn.addEventListener("click", () => {
        reportModal.classList.remove("modal-overlay--hidden");
    });
    cancelBtn.addEventListener("click", closeReportModal);
    reportModal.addEventListener("click", (e) => {
        if (e.target === reportModal) closeReportModal();
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const submitBtn = form.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        statusEl.textContent = t("Wysyłanie…");

        const formData = new FormData(form);
        formData.set("page_url", window.location.href);
        formData.set("browser_errors", collectErrorLog());

        try {
            const resp = await fetch("/report/submit", { method: "POST", body: formData });
            const data = await resp.json();

            if (data.ok) {
                statusEl.textContent = t("Wysłano. Dzięki!");
                setTimeout(() => {
                    closeReportModal();
                    form.reset();
                }, 1200);
            } else {
                statusEl.textContent = `${t("Błąd")}: ${t(data.error) ?? t("nieznany")}`;
            }
        } catch (err) {
            statusEl.textContent = t("Błąd sieci przy wysyłaniu.");
            console.error(err);
        } finally {
            submitBtn.disabled = false;
        }
    });
});

/*
Przełącznik motywu - optymistyczna zmiana w UI od razu po kliknięciu
(bez czekania na odpowiedź serwera), zapisywana asynchronicznie w tle
przez POST /settings/theme. Jeśli request się nie uda, motyw i tak
zostaje zmieniony lokalnie na czas tej wizyty - kolejne odświeżenie
strony wróci do wartości zapisanej w bazie.
*/
document.addEventListener("DOMContentLoaded", () => {
    const toggleBtn = document.getElementById("theme-toggle");
    if (!toggleBtn) return;

    toggleBtn.addEventListener("click", async () => {
        const html = document.documentElement;
        const newTheme = html.getAttribute("data-theme") === "light" ? "dark" : "light";
        html.setAttribute("data-theme", newTheme);

        try {
            await fetch("/settings/theme", { method: "POST" });
        } catch (err) {
            console.error("Nie udało się zapisać motywu:", err);
        }
    });
});

/*
Przełącznik demo/live w topbarze (2026-08-06) - patrz routes/settings.py::
toggle_environment. Świadomie confirmDialog() (własny modal appki), NIE
natywny confirm() - patrz [[feedback_snajper_no_native_confirm]] w pamięci
Claude, natywny "Anuluj" koliduje z przyciskiem appki o tej samej etykiecie.
Backend i tak ma własne bezpieczniki (klucz musi istnieć, boty muszą być
wyłączone) - to potwierdzenie to tylko "na pewno?", nie jedyna linia obrony.
*/
document.addEventListener("DOMContentLoaded", () => {
    const envBtn = document.getElementById("env-toggle");
    if (!envBtn) return;

    envBtn.addEventListener("click", async () => {
        const current = envBtn.classList.contains("topbar__env--live") ? "live" : "demo";
        const target = current === "live" ? "demo" : "live";
        const label = target === "live" ? t("PRAWDZIWE (LIVE)") : t("demo");

        const ok = await confirmDialog(`${t("Przełączyć konto na")} ${label}? ${t("Dotyczy WSZYSTKICH silników (Micro-Grid/Sygnał/EOD) i Warp Mode.")}`);
        if (!ok) return;

        try {
            const resp = await fetch("/settings/environment", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ environment: target }),
            });
            const data = await resp.json();
            if (!data.ok) {
                alert(t(data.error) || t("Nie udało się przełączyć środowiska."));
                return;
            }
            location.reload();
        } catch (err) {
            console.error("Nie udało się przełączyć środowiska:", err);
            alert(t("Błąd sieci - spróbuj ponownie."));
        }
    });
});

/*
Ukryj/pokaż wartości pieniężne (Adam, 2026-08-09: przycisk na Aktywach
chował TYLKO "Całość konta", ani "Wartość portfela" pod spodem, ani nic na
Warp Mode) - GLOBALNY przełącznik, jeden localStorage-owy stan dzielony
między wszystkimi stronami, zamiast osobnej kopii logiki per strona.

Konwencja: każda strona OZNACZA swoje elementy klasą "js-money" (statycznie
w HTML, albo dynamicznie przy (re)renderze przez JS - patrz portfolio.js/
warp.js) i ustawia im `dataset.real` na aktualną, prawdziwą wartość
tekstową. applyMoneyHiding() (wołana po KAŻDEJ zmianie tych elementów, bo
niektóre są całkowicie podmieniane przez innerHTML) tylko decyduje co
finalnie wyświetlić - stan "ukryte" przetrwa więc każde auto-odświeżenie
bez ponownego chowania. Przyciski przełączające mają klasę
"js-money-toggle-btn" (może być więcej niż jeden na stronie, np. Aktywa +
Warp) - wszystkie reagują na wspólny stan.
*/
const MONEY_HIDDEN_KEY = "snajper-hide-money";
const MONEY_HIDDEN_PLACEHOLDER = "•••••";
let moneyHidden = localStorage.getItem(MONEY_HIDDEN_KEY) === "1";

function applyMoneyHiding() {
    document.querySelectorAll(".js-money").forEach((el) => {
        if (el.dataset.real === undefined) el.dataset.real = el.textContent.trim();
        el.textContent = moneyHidden ? MONEY_HIDDEN_PLACEHOLDER : el.dataset.real;
    });
    document.querySelectorAll(".js-money-toggle-btn").forEach((btn) => {
        btn.classList.toggle("js-money-toggle-btn--hidden", moneyHidden);
        btn.title = moneyHidden ? t("Pokaż wartości") : t("Ukryj wartości (np. przed screen-share)");
    });
}

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".js-money-toggle-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            moneyHidden = !moneyHidden;
            localStorage.setItem(MONEY_HIDDEN_KEY, moneyHidden ? "1" : "0");
            applyMoneyHiding();
        });
    });
    applyMoneyHiding();
});

/*
Suwak glosnosci powiadomien dzwiekowych w Ustawieniach - patrz
settings_index.html. Element istnieje TYLKO na tej stronie, stad guard
"if (!slider) return" (ten sam wzorzec co theme-toggle wyzej, wspolny
common.js zamiast osobnego settings.js na jeden mały widget).
*/
document.addEventListener("DOMContentLoaded", () => {
    const slider = document.getElementById("sound-volume-slider");
    const valueLabel = document.getElementById("sound-volume-value");
    const testBtn = document.getElementById("sound-volume-test");
    if (!slider) return;

    slider.value = getSoundVolumePct();
    if (valueLabel) valueLabel.textContent = `${slider.value}%`;

    slider.addEventListener("input", () => {
        setSoundVolumePct(Number(slider.value));
        if (valueLabel) valueLabel.textContent = `${slider.value}%`;
    });

    if (testBtn) {
        testBtn.addEventListener("click", () => playSuccess());
    }
});

/*
setConnDot(status) - status: "ok" | "error" | "unknown"
"unknown" to stan początkowy (jeszcze nie sprawdzaliśmy) - szary, nie
czerwony, żeby nie sugerować fałszywie że coś już nie działa.
*/
function setConnDot(status) {
    const dot = document.querySelector("#t212-status-dot .topbar__conn-dot");
    const wrapper = document.getElementById("t212-status-dot");
    if (!dot || !wrapper) return;

    dot.classList.remove("topbar__conn-dot--ok", "topbar__conn-dot--error", "topbar__conn-dot--unknown");

    const labels = {
        ok: t("Połączenie z T212: OK"),
        error: t("Połączenie z T212: BŁĄD"),
        unknown: t("Status połączenia z T212 (nieznany)"),
    };
    dot.classList.add(`topbar__conn-dot--${status}`);
    wrapper.title = labels[status] || labels.unknown;
}

/*
=== Zakładki kategorii instrumentów (Akcje USD / Akcje EUR / Pozostałe) ===
Współdzielone między watchlist.js (panel "Wszystkie instrumenty"), pie.js
(wyszukiwarka "Dodaj aktywo" w Smart Virtual Pie) i bot.js - wszystkie czytają
ten sam lokalny cache instrumentów (services/instrument_cache.py), więc ten
sam zestaw zakładek i te same liczniki mają sens wszędzie. Podział WALUTOWY
akcji (poprawione 18.07.2026 - pierwsza wersja scaliła akcje w jedną
zakładkę, ale Adamowi chodziło o rozróżnienie USD/EUR, nie akcje/nie-akcje).
"Pozostałe" to worek na wszystko inne: ETF, ETF z dźwignią, Warrant, akcje w
innych walutach (GBX/CAD/CHF itd.). Plakietka "DŹWIGNIA" przy pojedynczych
wynikach (Instrument.is_leveraged) zostaje bez zmian - to ostrzeżenie
per-instrument, nie kategoria zakładki.
*/
const INSTRUMENT_CATEGORIES = [
    { id: "stock_usd", label: t("Akcje USD") },
    { id: "stock_eur", label: t("Akcje EUR") },
    { id: "other", label: t("Pozostałe") },
];

/*
instrumentTypeLabel(type) - "STOCK"/"ETF"/"WARRANT" (surowe pole z T212,
patrz Instrument.instrument_type) -> polska etykieta do wyników wyszukiwania.
Współdzielone między watchlist.js/pie.js/bot.js - patrz "rozszerzony opis"
w renderResults/renderAssetResults/renderBotAssetResults, dodany bo kilka
zupełnie ODDZIELNYCH spółek/notowań potrafi mieć identyczną krótką nazwę
(np. "Coca-Cola" - kilka niezależnych spółek-bottlerów, albo ta sama spółka
notowana w dwóch walutach na dwóch giełdach) - sam ticker+nazwa czasem nie
wystarczał, żeby je odróżnić.
*/
function instrumentTypeLabel(type) {
    const labels = { STOCK: t("Akcja"), ETF: "ETF", WARRANT: "Warrant" };
    return labels[type] || type || "?";
}

function renderCategoryTabs(container, counts, activeCategory, onSelect) {
    container.innerHTML = "";
    INSTRUMENT_CATEGORIES.forEach((cat) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tab" +
            (cat.id === activeCategory ? " tab--active" : "") +
            (cat.risk ? " tab--risk" : "");
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", cat.id === activeCategory ? "true" : "false");

        const count = counts ? counts[cat.id] : undefined;
        btn.innerHTML = count !== undefined
            ? `${cat.label} <span class="tab__count">${count}</span>`
            : cat.label;

        btn.addEventListener("click", () => onSelect(cat.id));
        container.appendChild(btn);
    });
}

/*
=== Sortowanie klikami w nagłówki - tabele STATYCZNE (bot/signal/eod pozycje) ===
Odpowiednik sortState/localStorage z portfolio.js (Aktywa, 27.07.2026), ale
uproszczony pod tabele, które NIE są okresowo podmieniane przez JS (bot.js/
signal.js/eod.js renderują "Otwarte pozycje" WYŁĄCZNIE przez Jinja przy
załadowaniu strony) - więc zamiast trzymać osobny model danych jak
currentPositions w portfolio.js, sortowanie działa wprost na wierszach DOM,
przestawiając <tr> w <tbody> przez porównanie data-sort-value na <td>
(ustawianym w szablonie - NIE parsujemy widocznego tekstu komórki, bo część
zawiera dodatkowy HTML: badge waluty, "(DCA 2)" itp.). Wybór sortu
zapisywany per-tabela w localStorage pod przekazanym storageKey, więc
przetrwa F5 i aplikuje się NATYCHMIAST przy starcie skryptu (ten sam fix co
w portfolio.js - zero opóźnienia/"skoku", bo tu i tak nie ma sieciowego
odświeżenia do poczekania).
*/
function initSortableTable(table, storageKey) {
    if (!table) return;
    const headerRow = table.querySelector("thead tr");
    const tbody = table.querySelector("tbody");
    if (!headerRow || !tbody) return;

    const headers = Array.from(headerRow.querySelectorAll("[data-sort-key]"));
    if (!headers.length) return;

    const sortState = { key: null, dir: 1 };
    try {
        const saved = JSON.parse(localStorage.getItem(storageKey));
        if (saved && saved.key) {
            sortState.key = saved.key;
            sortState.dir = saved.dir === -1 ? -1 : 1;
        }
    } catch (err) {
        // localStorage niedostępny/uszkodzony wpis - zostaje domyślny brak sortu
    }

    function columnIndex(key) {
        return Array.from(headerRow.children).findIndex((th) => th.dataset.sortKey === key);
    }

    function updateArrows() {
        headers.forEach((th) => {
            const active = th.dataset.sortKey === sortState.key;
            th.classList.toggle("history-table__th--sortable--active", active);
            th.classList.toggle("history-table__th--sortable--desc", active && sortState.dir === -1);
        });
    }

    function applySort() {
        updateArrows();
        if (!sortState.key) return;
        const idx = columnIndex(sortState.key);
        if (idx === -1) return;
        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.sort((a, b) => {
            const av = a.children[idx]?.dataset.sortValue ?? "";
            const bv = b.children[idx]?.dataset.sortValue ?? "";
            const an = parseFloat(av);
            const bn = parseFloat(bv);
            if (av !== "" && bv !== "" && !Number.isNaN(an) && !Number.isNaN(bn)) {
                return (an - bn) * sortState.dir;
            }
            const al = av.toLowerCase();
            const bl = bv.toLowerCase();
            return al < bl ? -sortState.dir : al > bl ? sortState.dir : 0;
        });
        rows.forEach((row) => tbody.appendChild(row));
    }

    function handleSort(th) {
        const key = th.dataset.sortKey;
        if (sortState.key === key) {
            sortState.dir *= -1;
        } else {
            sortState.key = key;
            sortState.dir = 1;
        }
        try {
            localStorage.setItem(storageKey, JSON.stringify({ key: sortState.key, dir: sortState.dir }));
        } catch (err) {
            // localStorage niedostępny (np. tryb prywatny) - sort działa, po prostu nie przetrwa F5
        }
        applySort();
    }

    // tabindex/keydown dopisane 2026-08-12 - <th> nie jest domyslnie
    // fokusowalny, wiec :focus-visible w style.css bez tego nigdy by sie nie
    // uruchomil (klikalny naglowek byl wczesniej WYLACZNIE mysza/dotykiem).
    headers.forEach((th) => {
        th.tabIndex = 0;
        th.addEventListener("click", () => handleSort(th));
        th.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                handleSort(th);
            }
        });
    });

    applySort();
}

// Trzy tabele "Otwarte pozycje" (bot/sygnał/eod) - initSortableTable sam
// nic nie robi gdy dana tabela nie istnieje w DOM (strona bez otwartych
// pozycji renderuje zamiast niej sam tekst), więc bezpieczne wołać wszystkie
// trzy tutaj zamiast osobno w bot.js/signal.js/eod.js.
document.addEventListener("DOMContentLoaded", () => {
    initSortableTable(document.getElementById("bot-positions-table"), "snajper-bot-positions-sort");
    initSortableTable(document.getElementById("signal-positions-table"), "snajper-signal-positions-sort");
    initSortableTable(document.getElementById("eod-positions-table"), "snajper-eod-positions-sort");
});

/*
=== Panele przestawialne strzalkami (bot/signal/eod) ===
Adam nie chcial JEDNEJ ustalonej na sztywno kolejnosci sekcji (Aktywa/
Otwarte pozycje/Aktywacja/Ustawienia ryzyka/Dziennik) - zamiast tego kazdy
user ustawia wlasna kolejnosc strzalkami gora/dol przy kazdym panelu
(27.07.2026, po tym jak proba zgadniecia "dobrej" kolejnosci nie trafila w
oczekiwania). Panele to BEZPOSREDNIE dzieci kontenera (div.reorderable-panel
z data-panel-id) - nav/bannery/stopka NIE sa panelami i zostaja na swoich
miejscach (przed/po). Kolejnosc zapamietywana w localStorage (tablica ID),
aplikowana NATYCHMIAST przy starcie skryptu - ten sam wzorzec co
initSortableTable (zero czekania, bez skoku po zaladowaniu).
*/
function initReorderablePanels(container, storageKey) {
    if (!container) return;
    const panels = Array.from(container.children).filter((el) => el.classList.contains("reorderable-panel"));
    if (!panels.length) return;

    function applyOrder(order) {
        const seen = new Set();
        order.forEach((id) => {
            const panel = panels.find((p) => p.dataset.panelId === id);
            if (panel) {
                container.appendChild(panel);
                seen.add(id);
            }
        });
        // Panele spoza zapisanej kolejnosci (np. dodane w kolejnej wersji appki
        // PO tym jak user juz raz poukladal swoje) - dolaczane na koniec,
        // zachowujac ich wzajemna kolejnosc z szablonu.
        panels.forEach((p) => {
            if (!seen.has(p.dataset.panelId)) container.appendChild(p);
        });
    }

    function saveOrder() {
        const order = Array.from(container.children)
            .filter((el) => el.classList.contains("reorderable-panel"))
            .map((p) => p.dataset.panelId);
        try {
            localStorage.setItem(storageKey, JSON.stringify(order));
        } catch (err) {
            // localStorage niedostepny - kolejnosc dziala w tej wizycie, nie przetrwa F5
        }
    }

    // Przyciski gora/dol na skrajnych panelach dotad wygladaly na klikalne,
    // ale klik na nich byl cichym no-opem (brak prev/next sibling) - teraz
    // dostaja realny atrybut disabled (:disabled w style.css juz na to czekal).
    function updateDisabledStates() {
        const ordered = Array.from(container.children).filter((el) => el.classList.contains("reorderable-panel"));
        ordered.forEach((panel, i) => {
            const upBtn = panel.querySelector('[data-move="up"]');
            const downBtn = panel.querySelector('[data-move="down"]');
            if (upBtn) upBtn.disabled = i === 0;
            if (downBtn) downBtn.disabled = i === ordered.length - 1;
        });
    }

    try {
        const saved = JSON.parse(localStorage.getItem(storageKey));
        if (Array.isArray(saved)) applyOrder(saved);
    } catch (err) {
        // brak/uszkodzony zapis - zostaje domyslna kolejnosc z szablonu
    }
    updateDisabledStates();

    container.addEventListener("click", (ev) => {
        const btn = ev.target.closest("[data-move]");
        if (!btn) return;
        const panel = btn.closest(".reorderable-panel");
        if (!panel) return;
        if (btn.dataset.move === "up") {
            const prev = panel.previousElementSibling;
            if (prev && prev.classList.contains("reorderable-panel")) container.insertBefore(panel, prev);
        } else {
            const next = panel.nextElementSibling;
            if (next && next.classList.contains("reorderable-panel")) container.insertBefore(next, panel);
        }
        saveOrder();
        updateDisabledStates();
    });
}

document.addEventListener("DOMContentLoaded", () => {
    initReorderablePanels(document.getElementById("bot-panel"), "snajper-bot-panel-order");
    initReorderablePanels(document.getElementById("signal-panel"), "snajper-signal-panel-order");
    initReorderablePanels(document.getElementById("eod-panel"), "snajper-eod-panel-order");
});

/*
Nawigacja sekcji strony (bot/signal/eod.html, ".tabs.section-nav") - to
zwykle linki kotwiczne (href="#section-x"), skok do sekcji dziala natywnie
bez JS. Brakowalo tylko podswietlenia klikniętej zakladki jako aktywnej -
ten sam, najprostszy wzorzec co gdzie indziej w apce (np. instrument.js
range-tabs): klik zdejmuje .tab--active z rodzenstwa, dodaje klikniętemu.
Bez scroll-spy - sekcje mozna dowolnie przestawiac strzalkami (patrz
initReorderablePanels wyzej), wiec "aktualnie widoczna sekcja" i tak nie
miala jednego stalego porzadku do sledzenia.
*/
document.querySelectorAll(".section-nav").forEach((nav) => {
    nav.addEventListener("click", (ev) => {
        const tab = ev.target.closest(".tab");
        if (!tab || !nav.contains(tab)) return;
        nav.querySelectorAll(".tab").forEach((t) => t.classList.remove("tab--active"));
        tab.classList.add("tab--active");
    });
});

async function fetchCategoryCounts() {
    try {
        const resp = await fetch("/settings/watchlist/category-counts");
        const data = await resp.json();
        return data.ok ? data.counts : null;
    } catch (err) {
        console.error("Nie udało się pobrać liczników kategorii:", err);
        return null;
    }
}

/*
Panel "Wiecej" dolnej belki nawigacji na telefonie (10.08.2026, patrz
base.html/.bottom-nav w style.css) - zwykly wysuwany panel, otwierany
przyciskiem #more-menu-btn, zamykany klikniеciem w tlo albo Escape.
"Zglos problem" w panelu NIE duplikuje logiki modala zgloszenia - tylko
przekazuje klikniеcie do juz istniejacego #report-problem-btn (desktopowy
przycisk w .topbar__links, ukryty na telefonie ale wciaz w DOM z pelnym
podpiеciem z bloku wyzej), zeby nie powielac kodu wysylki.
*/
document.addEventListener("DOMContentLoaded", () => {
    const menuBtn = document.getElementById("more-menu-btn");
    const overlay = document.getElementById("more-sheet-overlay");
    if (!menuBtn || !overlay) return;

    const open = () => {
        overlay.hidden = false;
        menuBtn.setAttribute("aria-expanded", "true");
    };
    const close = () => {
        overlay.hidden = true;
        menuBtn.setAttribute("aria-expanded", "false");
    };

    menuBtn.addEventListener("click", open);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !overlay.hidden) close();
    });

    const reportForwardBtn = document.getElementById("more-sheet-report-btn");
    const realReportBtn = document.getElementById("report-problem-btn");
    if (reportForwardBtn && realReportBtn) {
        reportForwardBtn.addEventListener("click", () => {
            close();
            realReportBtn.click();
        });
    }
});
