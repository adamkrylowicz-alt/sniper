"""
migrate_add_active_trade_sell_retry.py
========================================
Jednorazowa migracja: dokłada kolumny active_trades.sell_retry_count,
active_trades.next_sell_retry_at, active_trades.sell_blocked na istniejącej
bazie (instance/sniper.db) - patrz services/bot_engine.py::_retry_pending_sells.

Backfill dla wierszy sprzed tej migracji: sell_retry_count=0,
next_sell_retry_at=NULL (sprobuj przy najblizszym ticku), sell_blocked=0
(False) - domyslny, bezpieczny stan startowy, taki sam jak dla nowych pozycji.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_active_trade_sell_retry.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]

    added = []
    if "sell_retry_count" not in cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN sell_retry_count INTEGER NOT NULL DEFAULT 0")
        added.append("sell_retry_count")
    if "next_sell_retry_at" not in cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN next_sell_retry_at TEXT")
        added.append("next_sell_retry_at")
    if "sell_blocked" not in cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN sell_blocked INTEGER NOT NULL DEFAULT 0")
        added.append("sell_blocked")

    if not added:
        print("sell_retry_count/next_sell_retry_at/sell_blocked już istnieją w active_trades - nic do zrobienia.")
        conn.close()
        return

    conn.commit()
    conn.close()
    print(f"OK - dodano kolumny: {', '.join(added)}.")


if __name__ == "__main__":
    main()
