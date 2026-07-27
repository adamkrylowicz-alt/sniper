/*
bot.js
======
Panel Micro-Grid Bota. Aktywacja = DWIE bramki: hasło (weryfikowane
server-side, odszyfrowuje master_key) + confirmDialog (świadome
potwierdzenie, ten sam wzorzec co duże zlecenia w warp.js/pie.js) - to
uruchamia proces działający niezależnie od przeglądarki, więc przypadkowy
klik ma być trudniejszy niż zwykły przycisk.
*/

document.addEventListener("DOMContentLoaded", () => {
    const activateBtn = document.getElementById("btn-bot-activate");
    const deactivateBtn = document.getElementById("btn-bot-deactivate");
    const statusEl = document.getElementById("bot-activate-status");
    const passwordInput = document.getElementById("bot-password");

    activateBtn.addEventListener("click", async () => {
        const password = passwordInput.value;
        if (!password) {
            statusEl.textContent = "Podaj hasło.";
            return;
        }

        const confirmed = await confirmDialog(
            "Uruchomić bota? Poświadczenia zostaną w pamięci serwera dopóki " +
            "nie wyłączysz bota albo nie zrestartujesz appki. Bot działa na demo."
        );
        if (!confirmed) return;

        activateBtn.disabled = true;
        statusEl.textContent = "Uruchamianie...";

        try {
            const resp = await fetch("/bot/activate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password }),
            });
            const data = await resp.json();

            if (data.ok) {
                playSuccess();
                statusEl.textContent = "Bot uruchomiony.";
                window.location.reload();
            } else {
                playError();
                statusEl.textContent = data.error || "Nie udało się uruchomić.";
            }
        } catch (err) {
            playError();
            statusEl.textContent = "Błąd sieci.";
            console.error(err);
        } finally {
            activateBtn.disabled = false;
            passwordInput.value = "";
        }
    });

    deactivateBtn.addEventListener("click", async () => {
        const confirmed = await confirmDialog("Wyłączyć bota?");
        if (!confirmed) return;

        deactivateBtn.disabled = true;
        try {
            const resp = await fetch("/bot/deactivate", { method: "POST" });
            const data = await resp.json();
            if (data.ok) {
                playSuccess();
                window.location.reload();
            } else {
                playError();
                statusEl.textContent = data.error || "Nie udało się wyłączyć.";
            }
        } catch (err) {
            playError();
            statusEl.textContent = "Błąd sieci.";
            console.error(err);
        } finally {
            deactivateBtn.disabled = false;
        }
    });

    document.getElementById("btn-bot-save-settings").addEventListener("click", async () => {
        const btn = document.getElementById("btn-bot-save-settings");
        const settingsStatus = document.getElementById("bot-settings-status");
        btn.disabled = true;
        settingsStatus.textContent = "Zapisywanie...";

        const payload = {
            dca_scenario: document.getElementById("bot-dca-scenario").value,
            max_dca_levels: document.getElementById("bot-max-dca").value,
            dca_trigger_pct: document.getElementById("bot-dca-trigger").value,
            max_spread_pct: document.getElementById("bot-max-spread").value,
            take_profit_step_pct: document.getElementById("bot-take-profit-step").value,
            stop_loss_pct: document.getElementById("bot-stop-loss").value,
            max_daily_loss: document.getElementById("bot-max-daily-loss").value,
            is_paper_trading: document.getElementById("bot-paper-trading").checked,
            manage_all_positions: document.getElementById("bot-manage-all").checked,
        };

        try {
            const resp = await fetch("/bot/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (data.ok) {
                playSuccess();
                settingsStatus.textContent = data.released_count
                    ? `Zapisano. Zwolniono ${data.released_count} pozycji przejętych przez "zarządzaj wszystkim".`
                    : "Zapisano.";
            } else {
                playError();
                settingsStatus.textContent = data.error || "Błąd zapisu.";
            }
        } catch (err) {
            playError();
            settingsStatus.textContent = "Błąd sieci.";
            console.error(err);
        } finally {
            btn.disabled = false;
        }
    });

    // Wyczyść log (BUY/INFO/WARN - błędy już tu nie trafiają, patrz
    // bot_engine.py::_log, osobny plik instance/bot_errors.log).
    const clearLogBtn = document.getElementById("btn-bot-clear-log");
    if (clearLogBtn) {
        clearLogBtn.addEventListener("click", async () => {
            const logStatus = document.getElementById("bot-log-status");
            if (!(await confirmDialog("Wyczyścić cały dziennik bota? Tej operacji nie da się cofnąć."))) return;

            clearLogBtn.disabled = true;
            try {
                const resp = await fetch("/bot/log/clear", { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    const table = document.getElementById("bot-log-table");
                    if (table) table.remove();
                    let emptyHint = document.getElementById("bot-log-empty-hint");
                    if (!emptyHint) {
                        emptyHint = document.createElement("p");
                        emptyHint.className = "auth-box__hint";
                        emptyHint.id = "bot-log-empty-hint";
                        logStatus.insertAdjacentElement("afterend", emptyHint);
                    }
                    emptyHint.textContent = "Brak wpisów.";
                    logStatus.textContent = "Dziennik wyczyszczony.";
                } else {
                    playError();
                    logStatus.textContent = data.error || "Błąd czyszczenia logu.";
                }
            } catch (err) {
                playError();
                logStatus.textContent = "Błąd sieci.";
                console.error(err);
            } finally {
                clearLogBtn.disabled = false;
            }
        });
    }

    // Odblokowanie zablokowanej pozycji (sell_blocked=True) - delegacja na
    // tabeli, bo wierszy z przyciskiem moze byc kilka naraz.
    const positionsTable = document.getElementById("bot-positions-table");
    if (positionsTable) {
        positionsTable.addEventListener("click", async (ev) => {
            const btn = ev.target.closest("[data-unblock-position]");
            if (!btn) return;
            const row = btn.closest("tr[data-trade-id]");
            const tradeId = row?.dataset.tradeId;
            if (!tradeId) return;

            if (!(await confirmDialog("Odblokować tę pozycję? Bot spróbuje ponownie wystawić/przesunąć trailing STOP na najbliższym ticku."))) return;

            btn.disabled = true;
            try {
                const resp = await fetch(`/bot/positions/${tradeId}/unblock`, { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    const statusEl = row.querySelector(".bot-position-status");
                    statusEl.innerHTML = '<span class="history-table__status history-table__status--buy">OK</span>';
                } else {
                    playError();
                    btn.disabled = false;
                }
            } catch (err) {
                playError();
                console.error(err);
                btn.disabled = false;
            }
        });

        // "Zwolnij" - odwrotność adopcji (routes/bot.py::release_position):
        // pozycja znika z tabeli (nie jest juz status="OPEN"), udziały
        // zostają na koncie T212, bot przestaje ich pilnować.
        positionsTable.addEventListener("click", async (ev) => {
            const btn = ev.target.closest("[data-release-position]");
            if (!btn) return;
            const row = btn.closest("tr[data-trade-id]");
            const tradeId = row?.dataset.tradeId;
            if (!tradeId) return;

            if (!(await confirmDialog("Zwolnić tę pozycję spod zarządzania bota? Udziały ZOSTAJĄ na koncie T212 - tylko bot przestaje ich pilnować (trailing STOP zostanie anulowany)."))) return;

            btn.disabled = true;
            try {
                const resp = await fetch(`/bot/positions/${tradeId}/release`, { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    row.remove();
                } else {
                    playError();
                    window.alert(data.error || "Nie udało się zwolnić pozycji.");
                    btn.disabled = false;
                }
            } catch (err) {
                playError();
                console.error(err);
                btn.disabled = false;
            }
        });
    }

    /*
    === Aktywa bota - własna, niezależna od Pie lista (patrz models.py::BotAsset) ===
    Wyszukiwarka reużywa GET /settings/watchlist/search (lokalny cache
    instrumentów) i zakładki kategorii (common.js::renderCategoryTabs) - ten
    sam mechanizm co w pie.js, tylko "Dodaj" wymaga OD RAZU kwoty wejścia
    (pole "Kwota wejścia dla nowo dodawanych pozycji" nad wyszukiwarką) -
    w odróżnieniu od Pie, tu nie ma stanu pośredniego "dodane bez kwoty".
    */
    let botAssetSearchTimeout = null;
    let botAssetActiveCategory = "stock_usd";
    let botAssetCurrentOffset = 0;
    let botAssetCachedCounts = null;
    const BOT_ASSET_PAGE_SIZE = 20;

    function renderBotAssetResults(results, append) {
        const container = document.getElementById("bot-asset-results");
        if (!append) container.innerHTML = "";

        if (!append && results.length === 0) {
            container.innerHTML = '<p class="auth-box__hint">Brak wyników.</p>';
            return;
        }

        results.forEach((r) => {
            const row = document.createElement("div");
            row.className = "watchlist-results__item";

            const label = document.createElement("span");
            label.className = "watchlist-results__label";

            const mainLine = document.createElement("span");
            mainLine.textContent = `${r.name} — ${r.ticker.split("_")[0]}`;
            label.appendChild(mainLine);

            // Rozszerzony opis (typ + waluta) - patrz common.js::instrumentTypeLabel.
            const descLine = document.createElement("span");
            descLine.className = "watchlist-results__desc";
            descLine.textContent = `${instrumentTypeLabel(r.type)} · `;
            if (r.currency) {
                const currencyBadge = document.createElement("span");
                currencyBadge.className = `currency-badge currency-badge--${r.currency.toLowerCase()}`;
                currencyBadge.textContent = r.currency;
                descLine.appendChild(currencyBadge);
            } else {
                descLine.append("waluta nieznana");
            }
            label.appendChild(descLine);

            row.appendChild(label);

            if (r.is_leveraged) {
                const badge = document.createElement("span");
                badge.className = "badge badge--leverage";
                badge.textContent = "DŹWIGNIA";
                row.appendChild(badge);
            }

            const addBtn = document.createElement("button");
            addBtn.type = "button";
            addBtn.className = "watchlist-results__add";
            addBtn.textContent = "Dodaj";
            addBtn.addEventListener("click", () => addBotAsset(r.ticker, addBtn));
            row.appendChild(addBtn);

            container.appendChild(row);
        });
    }

    async function loadBotAssetResults(append) {
        const query = document.getElementById("bot-asset-search").value.trim();
        const offset = append ? botAssetCurrentOffset : 0;
        const loadMoreBtn = document.getElementById("bot-asset-load-more");

        try {
            const resp = await fetch(
                `/settings/watchlist/search?q=${encodeURIComponent(query)}&category=${botAssetActiveCategory}&offset=${offset}`
            );
            const data = await resp.json();
            const results = data.results || [];

            renderBotAssetResults(results, append);
            botAssetCurrentOffset = offset + results.length;
            loadMoreBtn.style.display = results.length < BOT_ASSET_PAGE_SIZE ? "none" : "";
        } catch (err) {
            console.error("Błąd wyszukiwania:", err);
        }
    }

    function selectBotAssetCategory(catId) {
        botAssetActiveCategory = catId;
        renderCategoryTabs(document.getElementById("bot-asset-tabs"), botAssetCachedCounts, botAssetActiveCategory, selectBotAssetCategory);
        loadBotAssetResults(false);
    }

    async function addBotAsset(ticker, addBtn) {
        const assetStatusEl = document.getElementById("bot-asset-status");
        const amountInput = document.getElementById("bot-asset-default-amount");
        const amount = amountInput.value;
        if (!amount || Number(amount) <= 0) {
            amountInput.focus();
            assetStatusEl.textContent = "Podaj najpierw kwotę wejścia (pole nad wyszukiwarką).";
            return;
        }

        addBtn.disabled = true;
        try {
            const resp = await fetch("/bot/assets/add", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ticker, entry_amount: amount }),
            });
            const data = await resp.json();
            if (!data.ok) {
                playError();
                assetStatusEl.textContent = data.error || "Nie udało się dodać.";
                addBtn.disabled = false;
                return;
            }
            playSuccess();
            window.location.reload();
        } catch (err) {
            playError();
            assetStatusEl.textContent = "Błąd sieci.";
            console.error(err);
            addBtn.disabled = false;
        }
    }

    const botAssetSearchInput = document.getElementById("bot-asset-search");
    if (botAssetSearchInput) {
        botAssetSearchInput.addEventListener("input", () => {
            clearTimeout(botAssetSearchTimeout);
            botAssetSearchTimeout = setTimeout(() => loadBotAssetResults(false), 300);
        });
    }

    const botAssetLoadMoreBtn = document.getElementById("bot-asset-load-more");
    if (botAssetLoadMoreBtn) {
        botAssetLoadMoreBtn.addEventListener("click", () => loadBotAssetResults(true));
    }

    if (document.getElementById("bot-asset-tabs")) {
        (async () => {
            botAssetCachedCounts = await fetchCategoryCounts();
            renderCategoryTabs(document.getElementById("bot-asset-tabs"), botAssetCachedCounts, botAssetActiveCategory, selectBotAssetCategory);
            loadBotAssetResults(false);
        })();
    }

    // -- Cena na żywo + przybliżona ilość akcji przy obecnej kwocie (patrz
    //    routes/bot.py::bot_asset_prices) - batch, jedno wywołanie na cały
    //    widok, tak jak /pie/<id>/charts w pie.js. ---------------------------
    async function loadBotAssetPrices() {
        try {
            const resp = await fetch("/bot/prices");
            const data = await resp.json();
            if (!data.ok) return;

            document.querySelectorAll("#bot-assets-list .pie-asset-row").forEach((row) => {
                const hintEl = row.querySelector("[data-price-hint]");
                const info = data.prices[row.dataset.ticker];
                if (!info || info.price === null) {
                    hintEl.textContent = "brak ceny (Finnhub/Yahoo niedostępne dla tego tickera)";
                    return;
                }
                const warningTxt = info.warning
                    ? " ⚠ może być za mało dla T212 (obserwowane minimum ~1.20 USD)"
                    : "";
                hintEl.textContent = `cena: ${info.price} · przy tej kwocie: ~${info.implied_quantity} akcji${warningTxt}`;
                hintEl.style.color = info.warning ? "var(--sell-red)" : "";
            });
        } catch (err) {
            console.error("Nie udało się pobrać cen:", err);
        }
    }

    if (document.getElementById("bot-assets-list")) {
        loadBotAssetPrices();
    }

    // -- Wiersze istniejących aktywów bota: kwota wejścia / penny / usuń --
    document.querySelectorAll("#bot-assets-list .pie-asset-row").forEach((row) => {
        const assetId = row.dataset.assetId;
        const rowStatus = row.querySelector("[data-status]");

        row.querySelector("[data-entry-amount]").addEventListener("change", async (event) => {
            try {
                const resp = await fetch(`/bot/asset/${assetId}/entry-amount`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ entry_amount: event.target.value }),
                });
                const data = await resp.json();
                if (!data.ok) {
                    playError();
                    rowStatus.textContent = data.error || "Nie udało się zapisać kwoty.";
                } else {
                    rowStatus.textContent = "Zapisano.";
                    loadBotAssetPrices();  // odśwież ostrzeżenie pod nową kwotę
                }
            } catch (err) {
                playError();
                rowStatus.textContent = "Błąd sieci.";
                console.error(err);
            }
        });

        const pennyBtn = row.querySelector("[data-penny-toggle]");
        pennyBtn.addEventListener("click", async () => {
            pennyBtn.disabled = true;
            try {
                const resp = await fetch(`/bot/asset/${assetId}/penny-toggle`, { method: "POST" });
                const data = await resp.json();
                if (!data.ok) {
                    playError();
                    rowStatus.textContent = data.error || "Nie udało się przełączyć.";
                    return;
                }
                pennyBtn.classList.toggle("pie-asset-row__icon-btn--active", data.is_penny_stock);
            } catch (err) {
                playError();
                rowStatus.textContent = "Błąd sieci.";
                console.error(err);
            } finally {
                pennyBtn.disabled = false;
            }
        });

        row.querySelector("[data-remove]").addEventListener("click", async () => {
            const confirmed = await confirmDialog(`Usunąć ${row.dataset.ticker} z listy bota?`);
            if (!confirmed) return;
            try {
                const resp = await fetch(`/bot/asset/${assetId}/remove`, { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    window.location.reload();
                } else {
                    rowStatus.textContent = data.error || "Nie udało się usunąć.";
                }
            } catch (err) {
                rowStatus.textContent = "Błąd sieci.";
                console.error(err);
            }
        });
    });
});
