"""
migrate_add_focus_tiles.py
============================
Jednorazowa migracja: dokłada kolumnę user_settings.focus_tiles na
istniejącej bazie (instance/sniper.db). Backfill = 1 dla wszystkich
istniejących wierszy (patrz models.py::UserSettings.focus_tiles, domyślnie
1 kafelek w Focus Mode).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_focus_tiles.py
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
    if "focus_tiles" in cols:
        print("focus_tiles już istnieje w user_settings - nic do zrobienia.")
        conn.close()
        return

    cur.execute("ALTER TABLE user_settings ADD COLUMN focus_tiles INTEGER NOT NULL DEFAULT 1")
    conn.commit()
    conn.close()
    print("OK - dodano user_settings.focus_tiles (domyślnie 1 dla wszystkich istniejących wierszy).")


if __name__ == "__main__":
    main()
