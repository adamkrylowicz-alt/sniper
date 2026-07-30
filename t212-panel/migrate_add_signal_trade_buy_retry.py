"""
migrate_add_signal_trade_buy_retry.py
========================================
Jednorazowa migracja: dokłada kolumny signal_trades.buy_retry_count i
signal_trades.next_buy_retry_at na istniejącej bazie (instance/sniper.db) -
patrz services/signal_engine.py::_retry_pending_buys ("gonienie" ceny LIMIT
BUY, dodane 2026-07-30 po realnym znalezisku - IFXd_EQ utknęło na 9+ godzin
bo Sygnał do tej pory nie miał żadnego mechanizmu ponawiania). Ten sam wzorzec
co migrate_add_active_trade_buy_retry.py (Micro-Grid).

Backfill dla wierszy sprzed tej migracji: buy_retry_count=0,
next_buy_retry_at=NULL (sprobuj przy najblizszym ticku) - domyslny, bezpieczny
stan startowy, taki sam jak dla nowych pozycji.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_signal_trade_buy_retry.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(signal_trades)")]

    added = []
    if "buy_retry_count" not in cols:
        cur.execute("ALTER TABLE signal_trades ADD COLUMN buy_retry_count INTEGER NOT NULL DEFAULT 0")
        added.append("buy_retry_count")
    if "next_buy_retry_at" not in cols:
        cur.execute("ALTER TABLE signal_trades ADD COLUMN next_buy_retry_at TEXT")
        added.append("next_buy_retry_at")

    if not added:
        print("buy_retry_count/next_buy_retry_at już istnieją w signal_trades - nic do zrobienia.")
        conn.close()
        return

    conn.commit()
    conn.close()
    print(f"OK - dodano kolumny: {', '.join(added)}.")


if __name__ == "__main__":
    main()
