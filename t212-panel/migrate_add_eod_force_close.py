"""
migrate_add_eod_force_close.py
=================================
Jednorazowa migracja: dokłada kolumnę eod_settings.force_close_enabled na
istniejącej bazie (instance/sniper.db) - patrz models.py::EODSettings i
services/eod_engine.py::_should_force_close.

Kontekst: moduł EOD pierwotnie miał wymuszone zamknięcie pozycji na koniec
sesji jako sztywne zachowanie (PRD: "nie przenoszą się na kolejny dzień").
Adam (2026-07-24, na żywo w trakcie testów) odrzucił to jako domyślne -
przerobione na opcjonalny przełącznik w Ustawieniach ryzyka, domyślnie
WYŁĄCZONY (pozycje zostają otwarte, zarządzane tylko stop-lossem/
take-profitem, jak w Sygnale).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_eod_force_close.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(eod_settings)")]

    if "force_close_enabled" in cols:
        print("force_close_enabled już istnieje w eod_settings - nic do zrobienia.")
        conn.close()
        return

    cur.execute("ALTER TABLE eod_settings ADD COLUMN force_close_enabled BOOLEAN NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()
    print("OK - dodano kolumnę force_close_enabled (domyślnie 0/False).")


if __name__ == "__main__":
    main()
