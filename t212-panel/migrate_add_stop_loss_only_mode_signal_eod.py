"""
migrate_add_stop_loss_only_mode_signal_eod.py
=================================================
Jednorazowa migracja: dokłada kolumnę stop_loss_only_mode do signal_settings
i eod_settings (domyślnie 0/False) - rozszerzenie trybu "tylko stop-loss"
(patrz migrate_add_stop_loss_only_mode.py dla Micro-Grid/risk_settings,
2026-08-06) na pozostałe dwa silniki, na życzenie Adama - "chce mieć
pewność że żaden z 3 silników nie otworzy nic nowego" przed podłączeniem
prawdziwego konta T212 do prod.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_stop_loss_only_mode_signal_eod.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column in cols:
        print(f"{column} już istnieje w {table} - nic do zrobienia.")
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} BOOLEAN NOT NULL DEFAULT 0")
    conn.commit()
    print(f"OK - dodano kolumnę {table}.{column} (domyślnie 0/False).")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    _add_column_if_missing(conn, "signal_settings", "stop_loss_only_mode")
    _add_column_if_missing(conn, "eod_settings", "stop_loss_only_mode")
    conn.close()


if __name__ == "__main__":
    main()
