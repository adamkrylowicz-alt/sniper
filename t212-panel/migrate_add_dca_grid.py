"""
migrate_add_dca_grid.py
=========================
Jednorazowa migracja: dokłada kolumny potrzebne pod pętlę DCA Micro-Grid Bota
(patrz services/bot_engine.py::_trigger_dca_buys/_confirm_dca_fills) -

risk_settings.dca_trigger_pct - % spadku ceny (wzgledem grid_anchor_price)
    wyzwalajacy kolejny poziom DCA. Backfill: 0.05 (5%, domyslny).

active_trades.grid_anchor_price - cena wejscia poziomu 0, staly punkt
    odniesienia siatki. Backfill: NULL-owe wiersze dostana 0 (istniejace
    pozycje sprzed tej migracji i tak nie beda mialy sensownej siatki DCA -
    trzeba by je recznie uzupelnic, jesli maja kontynuowac DCA).
active_trades.dca_pending_buy_order_id/dca_pending_quantity/
    dca_pending_price/dca_pending_baseline_quantity - stan zawieszonej nogi
    DCA w trakcie potwierdzania. Backfill: NULL (brak zawieszonej nogi).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_dca_grid.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    added = []

    risk_cols = [row[1] for row in cur.execute("PRAGMA table_info(risk_settings)")]
    if "dca_trigger_pct" not in risk_cols:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN dca_trigger_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.05")
        added.append("risk_settings.dca_trigger_pct")

    trade_cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]
    if "grid_anchor_price" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN grid_anchor_price NUMERIC(12, 4) NOT NULL DEFAULT 0")
        added.append("active_trades.grid_anchor_price")
    if "dca_pending_buy_order_id" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN dca_pending_buy_order_id VARCHAR(64)")
        added.append("active_trades.dca_pending_buy_order_id")
    if "dca_pending_quantity" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN dca_pending_quantity NUMERIC(12, 4)")
        added.append("active_trades.dca_pending_quantity")
    if "dca_pending_price" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN dca_pending_price NUMERIC(12, 4)")
        added.append("active_trades.dca_pending_price")
    if "dca_pending_baseline_quantity" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN dca_pending_baseline_quantity NUMERIC(12, 4)")
        added.append("active_trades.dca_pending_baseline_quantity")

    if not added:
        print("Wszystkie kolumny DCA już istnieją - nic do zrobienia.")
        conn.close()
        return

    conn.commit()
    conn.close()
    print(f"OK - dodano kolumny: {', '.join(added)}.")

    print(
        "\nUWAGA: istniejące OTWARTE pozycje (jeśli są) dostały grid_anchor_price=0 - "
        "to wyłączy dla nich DCA (0 * (1 - pct) = 0, cena nigdy realnie nie spadnie do 0). "
        "Jeśli mają kontynuować DCA, ustaw ręcznie grid_anchor_price = buy_price dla tych wierszy."
    )


if __name__ == "__main__":
    main()
