/*
signal.js
=========
Panel strategii sygnałowej RSI/MA/ATR - ten sam wzorzec co bot.js (Micro-Grid),
tylko inne endpointy/ID. Aktywacja = dwie bramki (hasło + confirmDialog), ten
sam powód: uruchamia proces działający niezależnie od przeglądarki.
*/

document.addEventListener("DOMContentLoaded", () => {
    const activateBtn = document.getElementById("btn-signal-activate");
    const deactivateBtn = document.getElementById("btn-signal-deactivate");
    const statusEl = document.getElementById("signal-activate-status");
    const passwordInput = document.getElementById("signal-password");

    activateBtn.addEventListener("click", async () => {
        const password = passwordInput.value;
        if (!password) {
            statusEl.textContent = "Podaj hasło.";
            return;
        }

        const confirmed = await confirmDialog(
            "Uruchomić strategię sygnałową? Poświadczenia zostaną w pamięci serwera " +
            "dopóki nie wyłączysz albo nie zrestartujesz appki. Działa na demo."
        );
        if (!confirmed) return;

        activateBtn.disabled = true;
        statusEl.textContent = "Uruchamianie...";

        try {
            const resp = await fetch("/signal/activate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password }),
            });
            const data = await resp.json();

            if (data.ok) {
                playSuccess();
                statusEl.textContent = "Uruchomiona.";
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
        const confirmed = await confirmDialog("Wyłączyć strategię sygnałową?");
        if (!confirmed) return;

        deactivateBtn.disabled = true;
        try {
            const resp = await fetch("/signal/deactivate", { method: "POST" });
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

    document.getElementById("btn-signal-save-settings").addEventListener("click", async () => {
        const btn = document.getElementById("btn-signal-save-settings");
        const settingsStatus = document.getElementById("signal-settings-status");
        btn.disabled = true;
        settingsStatus.textContent = "Zapisywanie...";

        const payload = {
            rsi_threshold: document.getElementById("signal-rsi-threshold").value,
            stop_loss_atr_mult: document.getElementById("signal-stop-loss-mult").value,
            take_profit_atr_mult: document.getElementById("signal-take-profit-mult").value,
            max_concurrent_positions: document.getElementById("signal-max-positions").value,
            is_paper_trading: document.getElementById("signal-paper-trading").checked,
            equity_sizing_enabled: document.getElementById("signal-equity-sizing").checked,
            fx_cost_adjustment_enabled: document.getElementById("signal-fx-cost-adjustment").checked,
            fx_fee_pct: document.getElementById("signal-fx-fee-pct").value,
        };

        try {
            const resp = await fetch("/signal/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (data.ok) {
                playSuccess();
                settingsStatus.textContent = "Zapisano.";
                if (data.equity_sizing_baseline !== undefined) {
                    document.getElementById("signal-equity-sizing-baseline").textContent = data.equity_sizing_baseline || "—";
                }
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

    const clearLogBtn = document.getElementById("btn-signal-clear-log");
    if (clearLogBtn) {
        clearLogBtn.addEventListener("click", async () => {
            const logStatus = document.getElementById("signal-log-status");
            if (!(await confirmDialog("Wyczyścić cały dziennik? Tej operacji nie da się cofnąć."))) return;

            clearLogBtn.disabled = true;
            try {
                const resp = await fetch("/signal/log/clear", { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    const table = document.getElementById("signal-log-table");
                    if (table) table.remove();
                    let emptyHint = document.getElementById("signal-log-empty-hint");
                    if (!emptyHint) {
                        emptyHint = document.createElement("p");
                        emptyHint.className = "auth-box__hint";
                        emptyHint.id = "signal-log-empty-hint";
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

    // Ręczne zamknięcie pozycji (siatka bezpieczeństwa - patrz routes/signal.py::close_position).
    const positionsTable = document.getElementById("signal-positions-table");
    if (positionsTable) {
        positionsTable.addEventListener("click", async (ev) => {
            const btn = ev.target.closest("[data-close-position]");
            if (!btn) return;
            const row = btn.closest("tr[data-trade-id]");
            const tradeId = row?.dataset.tradeId;
            if (!tradeId) return;

            if (!(await confirmDialog("Zamknąć tę pozycję teraz (Market Sell)? Stop-loss zostanie najpierw anulowany."))) return;

            btn.disabled = true;
            try {
                const resp = await fetch(`/signal/positions/${tradeId}/close`, { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    row.remove();
                } else {
                    playError();
                    window.alert(data.error || "Nie udało się zamknąć pozycji.");
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
    === Aktywa strategii - własna, niezależna lista (patrz models.py::SignalAsset) ===
    Ten sam mechanizm wyszukiwarki co bot.js/pie.js.
    */
    let assetSearchTimeout = null;
    let assetActiveCategory = "stock_usd";
    let assetCurrentOffset = 0;
    let assetCachedCounts = null;
    const ASSET_PAGE_SIZE = 20;

    function renderAssetResults(results, append) {
        const container = document.getElementById("signal-asset-results");
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
            addBtn.addEventListener("click", () => addSignalAsset(r.ticker, addBtn));
            row.appendChild(addBtn);

            container.appendChild(row);
        });
    }

    async function loadAssetResults(append) {
        const query = document.getElementById("signal-asset-search").value.trim();
        const offset = append ? assetCurrentOffset : 0;
        const loadMoreBtn = document.getElementById("signal-asset-load-more");

        try {
            const resp = await fetch(
                `/settings/watchlist/search?q=${encodeURIComponent(query)}&category=${assetActiveCategory}&offset=${offset}`
            );
            const data = await resp.json();
            const results = data.results || [];

            renderAssetResults(results, append);
            assetCurrentOffset = offset + results.length;
            loadMoreBtn.style.display = results.length < ASSET_PAGE_SIZE ? "none" : "";
        } catch (err) {
            console.error("Błąd wyszukiwania:", err);
        }
    }

    function selectAssetCategory(catId) {
        assetActiveCategory = catId;
        renderCategoryTabs(document.getElementById("signal-asset-tabs"), assetCachedCounts, assetActiveCategory, selectAssetCategory);
        loadAssetResults(false);
    }

    async function addSignalAsset(ticker, addBtn) {
        const assetStatusEl = document.getElementById("signal-asset-status");
        const amountInput = document.getElementById("signal-asset-default-amount");
        const amount = amountInput.value;
        if (!amount || Number(amount) <= 0) {
            amountInput.focus();
            assetStatusEl.textContent = "Podaj najpierw kwotę wejścia (pole nad wyszukiwarką).";
            return;
        }

        addBtn.disabled = true;
        try {
            const resp = await fetch("/signal/assets/add", {
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

    const assetSearchInput = document.getElementById("signal-asset-search");
    if (assetSearchInput) {
        assetSearchInput.addEventListener("input", () => {
            clearTimeout(assetSearchTimeout);
            assetSearchTimeout = setTimeout(() => loadAssetResults(false), 300);
        });
    }

    const assetLoadMoreBtn = document.getElementById("signal-asset-load-more");
    if (assetLoadMoreBtn) {
        assetLoadMoreBtn.addEventListener("click", () => loadAssetResults(true));
    }

    if (document.getElementById("signal-asset-tabs")) {
        (async () => {
            assetCachedCounts = await fetchCategoryCounts();
            renderCategoryTabs(document.getElementById("signal-asset-tabs"), assetCachedCounts, assetActiveCategory, selectAssetCategory);
            loadAssetResults(false);
        })();
    }

    // -- Cena na żywo + przybliżona ilość akcji (patrz routes/signal.py::asset_prices) --
    async function loadAssetPrices() {
        try {
            const resp = await fetch("/signal/prices");
            const data = await resp.json();
            if (!data.ok) return;

            document.querySelectorAll("#signal-assets-list .pie-asset-row").forEach((row) => {
                const hintEl = row.querySelector("[data-price-hint]");
                const info = data.prices[row.dataset.ticker];
                if (!info || info.price === null) {
                    hintEl.textContent = "brak ceny (Finnhub/Yahoo niedostępne dla tego tickera)";
                    return;
                }
                hintEl.textContent = `cena: ${info.price} · przy tej kwocie: ~${info.implied_quantity} akcji`;
            });
        } catch (err) {
            console.error("Nie udało się pobrać cen:", err);
        }
    }

    if (document.getElementById("signal-assets-list")) {
        loadAssetPrices();
    }

    // -- Wiersze istniejących aktywów: kwota wejścia / usuń --
    document.querySelectorAll("#signal-assets-list .pie-asset-row").forEach((row) => {
        const assetId = row.dataset.assetId;
        const rowStatus = row.querySelector("[data-status]");

        row.querySelector("[data-entry-amount]").addEventListener("change", async (event) => {
            try {
                const resp = await fetch(`/signal/asset/${assetId}/entry-amount`, {
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
                    loadAssetPrices();
                }
            } catch (err) {
                playError();
                rowStatus.textContent = "Błąd sieci.";
                console.error(err);
            }
        });

        row.querySelector("[data-remove]").addEventListener("click", async () => {
            const confirmed = await confirmDialog(`Usunąć ${row.dataset.ticker} z listy?`);
            if (!confirmed) return;
            try {
                const resp = await fetch(`/signal/asset/${assetId}/remove`, { method: "POST" });
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
