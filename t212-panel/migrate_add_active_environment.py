"""
migrate_add_active_environment.py
=====================================
Jednorazowa migracja: dokłada kolumnę user_settings.active_environment
(domyślnie "demo") - przełącznik demo/live per-user (2026-08-06, Adam:
"przełącz na live... i dodaj guzik przełącznik live demo"), zastępuje
dawne stałe modułowe BOT_ENVIRONMENT/SIGNAL_ENVIRONMENT/EOD_ENVIRONMENT
(zawsze "demo" na sztywno) - patrz utils.py::current_environment.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_active_environment.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(user_settings)")]
    if "active_environment" in cols:
        print("active_environment już istnieje w user_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE user_settings ADD COLUMN active_environment VARCHAR(10) NOT NULL DEFAULT 'demo'")
        conn.commit()
        print("OK - dodano kolumnę user_settings.active_environment (domyślnie 'demo').")

    conn.close()


if __name__ == "__main__":
    main()
