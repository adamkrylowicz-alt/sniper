"""
migrate_add_equity_sizing.py
=============================
Jednorazowa migracja: dokłada dwie kolumny na istniejącej bazie
(instance/sniper.db) dla money management opartego o skalowanie √equity
(Micro-Grid Bot, 2026-07-31 - patrz docs/IDEAS_v2.md, seria "Build Better
Strategies" część 3 - sqrt zamiast liniowego %equity, odrzucone Kelly/OptimalF):

- risk_settings.equity_sizing_enabled (globalny przełącznik, domyślnie 0)
- risk_settings.equity_sizing_baseline (equity odniesienia, auto-capture przy
  włączeniu powyższego - patrz routes/bot.py::update_settings, NULL dopóki
  nikt nie włączył ani razu)

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_equity_sizing.py
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

    if "equity_sizing_enabled" in risk_cols:
        print("equity_sizing_enabled już istnieje w risk_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN equity_sizing_enabled BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()
        print("OK - dodano kolumnę risk_settings.equity_sizing_enabled (domyślnie 0/False).")

    if "equity_sizing_baseline" in risk_cols:
        print("equity_sizing_baseline już istnieje w risk_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN equity_sizing_baseline NUMERIC(12, 2)")
        conn.commit()
        print("OK - dodano kolumnę risk_settings.equity_sizing_baseline (NULL - brak dopóki nikt nie włączy flagi).")

    conn.close()


if __name__ == "__main__":
    main()
