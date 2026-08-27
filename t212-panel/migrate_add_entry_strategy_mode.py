"""
migrate_add_entry_strategy_mode.py
=====================================
Jednorazowa migracja: dokłada kolumnę risk_settings.entry_strategy_mode
(domyślnie "classic") - selektor strategii wejścia Micro-Gridu (2026-08-27,
"RSI Hybrid" jako opcja obok klasycznego scoringu), patrz models.py::RiskSettings
i bot_engine.py::_process_entries/_manage_trailing_exit.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_entry_strategy_mode.py
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
    if "entry_strategy_mode" in risk_cols:
        print("entry_strategy_mode już istnieje w risk_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN entry_strategy_mode VARCHAR(20) NOT NULL DEFAULT 'classic'")
        conn.commit()
        print("OK - dodano kolumnę risk_settings.entry_strategy_mode (domyślnie 'classic').")

    conn.close()


if __name__ == "__main__":
    main()
