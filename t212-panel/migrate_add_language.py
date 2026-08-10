"""
migrate_add_language.py
=========================
Jednorazowa migracja: dokłada kolumnę user_settings.language (domyślnie
"pl") - przełącznik PL/EN (2026-08-10, Adam: appka ma być czytelna dla
kogoś kto nie zna polskiego), patrz models.py::UserSettings.language i
app/i18n.py.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_language.py
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
    if "language" in cols:
        print("language już istnieje w user_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE user_settings ADD COLUMN language VARCHAR(5) NOT NULL DEFAULT 'pl'")
        conn.commit()
        print("OK - dodano kolumnę user_settings.language (domyślnie 'pl').")

    conn.close()


if __name__ == "__main__":
    main()
