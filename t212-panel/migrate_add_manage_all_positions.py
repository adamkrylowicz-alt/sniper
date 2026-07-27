"""
migrate_add_manage_all_positions.py
=====================================
Jednorazowa migracja: dokłada dwie kolumny na istniejącej bazie
(instance/sniper.db) dla switcha "zarządzaj wszystkim" (pomysł #1,
docs/IDEAS_v2.md, zaimplementowany 27.07.2026):

- risk_settings.manage_all_positions (globalny przełącznik, domyślnie 0)
- active_trades.auto_adopted (odróżnia pozycje automatycznie przejęte przez
  switch od ręcznej adopcji przyciskiem "Przekaż botowi" - patrz
  bot_engine.py::_auto_adopt_foreign_positions i
  routes/bot.py::_release_auto_adopted_positions)

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_manage_all_positions.py
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
    if "manage_all_positions" in risk_cols:
        print("manage_all_positions już istnieje w risk_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN manage_all_positions BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()
        print("OK - dodano kolumnę risk_settings.manage_all_positions (domyślnie 0/False).")

    trade_cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]
    if "auto_adopted" in trade_cols:
        print("auto_adopted już istnieje w active_trades - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE active_trades ADD COLUMN auto_adopted BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()
        print("OK - dodano kolumnę active_trades.auto_adopted (domyślnie 0/False).")

    conn.close()


if __name__ == "__main__":
    main()
