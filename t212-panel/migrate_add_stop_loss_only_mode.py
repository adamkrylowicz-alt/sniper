"""
migrate_add_stop_loss_only_mode.py
=====================================
Jednorazowa migracja: dokłada kolumnę risk_settings.stop_loss_only_mode
(domyślnie 0/False) - tryb "tylko stop-loss" (2026-08-06, na życzenie Adama:
"wyłącz wszystkie funkcjonalności bota poza jedną - ma ustawiać stoplossa na
aktywa które mu wskażę"), patrz models.py::RiskSettings i
bot_engine.py::tick().

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_stop_loss_only_mode.py
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
    if "stop_loss_only_mode" in risk_cols:
        print("stop_loss_only_mode już istnieje w risk_settings - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE risk_settings ADD COLUMN stop_loss_only_mode BOOLEAN NOT NULL DEFAULT 0")
        conn.commit()
        print("OK - dodano kolumnę risk_settings.stop_loss_only_mode (domyślnie 0/False).")

    conn.close()


if __name__ == "__main__":
    main()
