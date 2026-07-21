"""
migrate_add_trailing_exit.py
===============================
Jednorazowa migracja: przechodzi ze sztywnego LIMIT SELL po stałej kwocie
(RiskSettings.take_profit_usd) na przesuwający się w górę LIMIT SELL (co
take_profit_step_pct) + STOP loss uzbrajany po przekroczeniu 2 kroków - patrz
services/bot_engine.py::_manage_trailing_exit (ustalone z Adamem 2026-07-21:
"nie wystawiaj stałego zlecenia, tylko przesuwaj co krok, a jak wejdzie w
zysk to uzbrój stop-loss").

Dokłada:
- risk_settings.take_profit_step_pct NUMERIC(6,4) DEFAULT 0.003 (0.3%, krok
  przesuwania LIMIT SELL - PROCENT ceny, nie stała kwota, żeby skalowało się
  z ceną instrumentu, patrz uzasadnienie Adama o ASML vs groszówka).
- risk_settings.stop_loss_pct NUMERIC(6,4) DEFAULT 0.02 (2%, od average_price,
  uzbrajany dopiero po 2 krokach take-profit).
- active_trades.buy_confirmed BOOLEAN DEFAULT 0 - zastępuje stare
  "sell_order_id IS NOT NULL" jako sygnał "kupno rozliczone, można zarządzać
  wyjściem". Backfill: 1 dla wierszy które już miały sell_order_id (pod
  starym schematem to oznaczało dokładnie to samo).
- active_trades.trail_milestone_steps INTEGER DEFAULT 0 - ile kroków
  take-profit już uzbrojono (0 = jeszcze żaden).
- active_trades.stop_order_id VARCHAR(64) NULL - druga (ochronna) noga obok
  sell_order_id, ręczne OCO (patrz reconcile()).

risk_settings.take_profit_usd NIE jest usuwana - SQLite 3.34.1 na tym NAS-ie
nie wspiera ALTER TABLE DROP COLUMN (dodano w 3.35). Kolumna zostaje jako
martwa (kod jej już nigdzie nie czyta), zamiast ryzykować przebudowę tabeli
na żywej bazie produkcyjnej dla czysto kosmetycznego zysku.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_trailing_exit.py
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

    risk_cols = [row[1] for row in cur.execute("PRAGMA table_info(risk_settings)")]
    trade_cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]

    added = []

    if "take_profit_step_pct" not in risk_cols:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN take_profit_step_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.003")
        added.append("risk_settings.take_profit_step_pct")

    if "stop_loss_pct" not in risk_cols:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN stop_loss_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.02")
        added.append("risk_settings.stop_loss_pct")

    if "buy_confirmed" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN buy_confirmed INTEGER NOT NULL DEFAULT 0")
        cur.execute("UPDATE active_trades SET buy_confirmed = 1 WHERE sell_order_id IS NOT NULL")
        added.append("active_trades.buy_confirmed (z backfillem)")

    if "trail_milestone_steps" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN trail_milestone_steps INTEGER NOT NULL DEFAULT 0")
        added.append("active_trades.trail_milestone_steps")

    if "stop_order_id" not in trade_cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN stop_order_id VARCHAR(64)")
        added.append("active_trades.stop_order_id")

    if not added:
        print("Wszystkie kolumny już istnieją - nic do zrobienia.")
        conn.close()
        return

    conn.commit()
    conn.close()
    print(f"OK - dodano: {', '.join(added)}.")


if __name__ == "__main__":
    main()
