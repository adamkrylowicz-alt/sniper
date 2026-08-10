/*
eod.js
======
Panel modułu EOD (koniec sesji) - ten sam wzorzec co signal.js/bot.js, tylko
inne endpointy/ID. Aktywacja = dwie bramki (hasło + confirmDialog).
*/

document.addEventListener("DOMContentLoaded", () => {
    const activateBtn = document.getElementById("btn-eod-activate");
    const deactivateBtn = document.getElementById("btn-eod-deactivate");
    const statusEl = document.getElementById("eod-activate-status");
    const passwordInput = document.getElementById("eod-password");

    activateBtn.addEventListener("click", async () => {
        const password = passwordInput.value;
        if (!password) {
            statusEl.textContent = t("Podaj hasło.");
            return;
        }

        const confirmed = await confirmDialog(
            t("Uruchomić moduł EOD? Poświadczenia zostaną w pamięci serwera dopóki nie wyłączysz albo nie zrestartujesz appki. Działa na demo.")
        );
        if (!confirmed) return;

        activateBtn.disabled = true;
        statusEl.textContent = t("Uruchamianie...");

        try {
            const resp = await fetch("/eod/activate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password }),
            });
            const data = await resp.json();

            if (data.ok) {
                playSuccess();
                statusEl.textContent = t("Uruchomiony.");
                window.location.reload();
            } else {
                playError();
                statusEl.textContent = t(data.error) || t("Nie udało się uruchomić.");
            }
        } catch (err) {
            playError();
            statusEl.textContent = t("Błąd sieci.");
            console.error(err);
        } finally {
            activateBtn.disabled = false;
            passwordInput.value = "";
        }
    });

    deactivateBtn.addEventListener("click", async () => {
        const confirmed = await confirmDialog(t("Wyłączyć moduł EOD?"));
        if (!confirmed) return;

        deactivateBtn.disabled = true;
        try {
            const resp = await fetch("/eod/deactivate", { method: "POST" });
            const data = await resp.json();
            if (data.ok) {
                playSuccess();
                window.location.reload();
            } else {
                playError();
                statusEl.textContent = t(data.error) || t("Nie udało się wyłączyć.");
            }
        } catch (err) {
            playError();
            statusEl.textContent = t("Błąd sieci.");
            console.error(err);
        } finally {
            deactivateBtn.disabled = false;
        }
    });

    document.getElementById("btn-eod-save-settings").addEventListener("click", async () => {
        const btn = document.getElementById("btn-eod-save-settings");
        const settingsStatus = document.getElementById("eod-settings-status");
        btn.disabled = true;
        settingsStatus.textContent = t("Zapisywanie...");

        const payload = {
            stop_loss_pct: document.getElementById("eod-stop-loss").value,
            take_profit_pct: document.getElementById("eod-take-profit").value,
            max_concurrent_positions: document.getElementById("eod-max-positions").value,
            is_paper_trading: document.getElementById("eod-paper-trading").checked,
            force_close_enabled: document.getElementById("eod-force-close").checked,
            stop_loss_only_mode: document.getElementById("eod-stop-loss-only").checked,
            equity_sizing_enabled: document.getElementById("eod-equity-sizing").checked,
            fx_cost_adjustment_enabled: document.getElementById("eod-fx-cost-adjustment").checked,
            fx_fee_pct: document.getElementById("eod-fx-fee-pct").value,
        };

        try {
            const resp = await fetch("/eod/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (data.ok) {
                playSuccess();
                settingsStatus.textContent = t("Zapisano.");
                if (data.equity_sizing_baseline !== undefined) {
                    document.getElementById("eod-equity-sizing-baseline").textContent = data.equity_sizing_baseline || "—";
                }
            } else {
                playError();
                settingsStatus.textContent = t(data.error) || t("Błąd zapisu.");
            }
        } catch (err) {
            playError();
            settingsStatus.textContent = t("Błąd sieci.");
            console.error(err);
        } finally {
            btn.disabled = false;
        }
    });

    const clearLogBtn = document.getElementById("btn-eod-clear-log");
    if (clearLogBtn) {
        clearLogBtn.addEventListener("click", async () => {
            const logStatus = document.getElementById("eod-log-status");
            if (!(await confirmDialog(t("Wyczyścić cały dziennik? Tej operacji nie da się cofnąć.")))) return;

            clearLogBtn.disabled = true;
            try {
                const resp = await fetch("/eod/log/clear", { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    const table = document.getElementById("eod-log-table");
                    if (table) table.remove();
                    let emptyHint = document.getElementById("eod-log-empty-hint");
                    if (!emptyHint) {
                        emptyHint = document.createElement("p");
                        emptyHint.className = "auth-box__hint";
                        emptyHint.id = "eod-log-empty-hint";
                        logStatus.insertAdjacentElement("afterend", emptyHint);
                    }
                    emptyHint.textContent = t("Brak wpisów.");
                    logStatus.textContent = t("Dziennik wyczyszczony.");
                } else {
                    playError();
                    logStatus.textContent = t(data.error) || t("Błąd czyszczenia logu.");
                }
            } catch (err) {
                playError();
                logStatus.textContent = t("Błąd sieci.");
                console.error(err);
            } finally {
                clearLogBtn.disabled = false;
            }
        });
    }

    // Ręczne zamknięcie pozycji (siatka bezpieczeństwa - patrz routes/eod.py::close_position).
    const positionsTable = document.getElementById("eod-positions-table");
    if (positionsTable) {
        positionsTable.addEventListener("click", async (ev) => {
            const btn = ev.target.closest("[data-close-position]");
            if (!btn) return;
            const row = btn.closest("tr[data-trade-id]");
            const tradeId = row?.dataset.tradeId;
            if (!tradeId) return;

            if (!(await confirmDialog(t("Zamknąć tę pozycję teraz (Market Sell)? Stop-loss zostanie najpierw anulowany.")))) return;

            btn.disabled = true;
            try {
                const resp = await fetch(`/eod/positions/${tradeId}/close`, { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    playSuccess();
                    row.remove();
                } else {
                    playError();
                    window.alert(t(data.error) || t("Nie udało się zamknąć pozycji."));
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
    === Aktywa modułu EOD - własna, niezależna lista (patrz models.py::EODAsset) ===
    Ten sam mechanizm wyszukiwarki co bot.js/signal.js/pie.js.
    */
    let assetSearchTimeout = null;
    let assetActiveCategory = "stock_usd";
    let assetCurrentOffset = 0;
    let assetCachedCounts = null;
    const ASSET_PAGE_SIZE = 20;

    function renderAssetResults(results, append) {
        const container = document.getElementById("eod-asset-results");
        if (!append) container.innerHTML = "";

        if (!append && results.length === 0) {
            container.innerHTML = `<p class="auth-box__hint">${t("Brak wyników.")}</p>`;
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
                descLine.append(t("waluta nieznana"));
            }
            label.appendChild(descLine);

            row.appendChild(label);

            if (r.is_leveraged) {
                const badge = document.createElement("span");
                badge.className = "badge badge--leverage";
                badge.textContent = t("DŹWIGNIA");
                row.appendChild(badge);
            }

            const addBtn = document.createElement("button");
            addBtn.type = "button";
            addBtn.className = "watchlist-results__add";
            addBtn.textContent = t("Dodaj");
            addBtn.addEventListener("click", () => addEodAsset(r.ticker, addBtn));
            row.appendChild(addBtn);

            container.appendChild(row);
        });
    }

    async function loadAssetResults(append) {
        const query = document.getElementById("eod-asset-search").value.trim();
        const offset = append ? assetCurrentOffset : 0;
        const loadMoreBtn = document.getElementById("eod-asset-load-more");

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
        renderCategoryTabs(document.getElementById("eod-asset-tabs"), assetCachedCounts, assetActiveCategory, selectAssetCategory);
        loadAssetResults(false);
    }

    async function addEodAsset(ticker, addBtn) {
        const assetStatusEl = document.getElementById("eod-asset-status");
        const amountInput = document.getElementById("eod-asset-default-amount");
        const amount = amountInput.value;
        if (!amount || Number(amount) <= 0) {
            amountInput.focus();
            assetStatusEl.textContent = t("Podaj najpierw kwotę wejścia (pole nad wyszukiwarką).");
            return;
        }

        addBtn.disabled = true;
        try {
            const resp = await fetch("/eod/assets/add", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ticker, entry_amount: amount }),
            });
            const data = await resp.json();
            if (!data.ok) {
                playError();
                assetStatusEl.textContent = t(data.error) || t("Nie udało się dodać.");
                addBtn.disabled = false;
                return;
            }
            playSuccess();
            window.location.reload();
        } catch (err) {
            playError();
            assetStatusEl.textContent = t("Błąd sieci.");
            console.error(err);
            addBtn.disabled = false;
        }
    }

    const assetSearchInput = document.getElementById("eod-asset-search");
    if (assetSearchInput) {
        assetSearchInput.addEventListener("input", () => {
            clearTimeout(assetSearchTimeout);
            assetSearchTimeout = setTimeout(() => loadAssetResults(false), 300);
        });
    }

    const assetLoadMoreBtn = document.getElementById("eod-asset-load-more");
    if (assetLoadMoreBtn) {
        assetLoadMoreBtn.addEventListener("click", () => loadAssetResults(true));
    }

    if (document.getElementById("eod-asset-tabs")) {
        (async () => {
            assetCachedCounts = await fetchCategoryCounts();
            renderCategoryTabs(document.getElementById("eod-asset-tabs"), assetCachedCounts, assetActiveCategory, selectAssetCategory);
            loadAssetResults(false);
        })();
    }

    // -- Cena na żywo + przybliżona ilość akcji (patrz routes/eod.py::asset_prices) --
    async function loadAssetPrices() {
        try {
            const resp = await fetch("/eod/prices");
            const data = await resp.json();
            if (!data.ok) return;

            document.querySelectorAll("#eod-assets-list .pie-asset-row").forEach((row) => {
                const hintEl = row.querySelector("[data-price-hint]");
                const info = data.prices[row.dataset.ticker];
                if (!info || info.price === null) {
                    hintEl.textContent = t("brak ceny (Finnhub/Yahoo niedostępne dla tego tickera)");
                    return;
                }
                hintEl.textContent = `${t("cena:")} ${info.price} · ${t("przy bazowej kwocie:")} ~${info.implied_quantity} ${t("akcji")}`;
            });
        } catch (err) {
            console.error("Nie udało się pobrać cen:", err);
        }
    }

    if (document.getElementById("eod-assets-list")) {
        loadAssetPrices();
    }

    // -- Wiersze istniejących aktywów: kwota wejścia / usuń --
    document.querySelectorAll("#eod-assets-list .pie-asset-row").forEach((row) => {
        const assetId = row.dataset.assetId;
        const rowStatus = row.querySelector("[data-status]");

        row.querySelector("[data-entry-amount]").addEventListener("change", async (event) => {
            try {
                const resp = await fetch(`/eod/asset/${assetId}/entry-amount`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ entry_amount: event.target.value }),
                });
                const data = await resp.json();
                if (!data.ok) {
                    playError();
                    rowStatus.textContent = t(data.error) || t("Nie udało się zapisać kwoty.");
                } else {
                    rowStatus.textContent = t("Zapisano.");
                    loadAssetPrices();
                }
            } catch (err) {
                playError();
                rowStatus.textContent = t("Błąd sieci.");
                console.error(err);
            }
        });

        row.querySelector("[data-remove]").addEventListener("click", async () => {
            const confirmed = await confirmDialog(`${t("Usunąć")} ${row.dataset.ticker} ${t("z listy?")}`);
            if (!confirmed) return;
            try {
                const resp = await fetch(`/eod/asset/${assetId}/remove`, { method: "POST" });
                const data = await resp.json();
                if (data.ok) {
                    window.location.reload();
                } else {
                    rowStatus.textContent = t(data.error) || t("Nie udało się usunąć.");
                }
            } catch (err) {
                rowStatus.textContent = t("Błąd sieci.");
                console.error(err);
            }
        });
    });
});
