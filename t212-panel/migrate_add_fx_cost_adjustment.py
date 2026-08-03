"""
migrate_add_fx_cost_adjustment.py
====================================
Jednorazowa migracja: dokłada kolumny fx_cost_adjustment_enabled/fx_fee_pct
na trzech tabelach ustawień (risk_settings/signal_settings/eod_settings) -
koszt przewalutowania (FX) dla tickerów USD na koncie EUR (patrz
docs/IDEAS_v2.md, 2026-08-03 - "spr dlaczego mam 13 aktywow" -> Order
Execution Policy T212 pkt 17.3 + potwierdzone przez API że konto demo ma
JEDNĄ walutę).

Micro-Grid (risk_settings) miał ten koszt ZAWSZE aktywny na sztywno
(FX_ROUND_TRIP_PCT w bot_engine.py, od 22.07.2026) - migracja ustawia
enabled=1 tam, żeby NIC się nie zmieniło w dotychczasowym zachowaniu.
Sygnał/EOD nigdy tego nie miały - Adam poprosił o dodanie z domyślnie
WŁĄCZONYM przełącznikiem (to naprawa realnej luki, nie eksperyment).

fx_fee_pct domyślnie 0.0015 (0.15% za JEDNĄ nogę konwersji, ~0.3% round-trip)
wszędzie - ta sama wartość co dotychczasowa stała FX_FEE_PCT w bot_engine.py.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_fx_cost_adjustment.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"

TABLES = ["risk_settings", "signal_settings", "eod_settings"]


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for table in TABLES:
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]

        if "fx_cost_adjustment_enabled" in cols:
            print(f"{table}.fx_cost_adjustment_enabled już istnieje - nic do zrobienia.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN fx_cost_adjustment_enabled BOOLEAN NOT NULL DEFAULT 1")
            conn.commit()
            print(f"OK - dodano kolumnę {table}.fx_cost_adjustment_enabled (domyślnie 1/True).")

        if "fx_fee_pct" in cols:
            print(f"{table}.fx_fee_pct już istnieje - nic do zrobienia.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN fx_fee_pct NUMERIC(6, 4) NOT NULL DEFAULT 0.0015")
            conn.commit()
            print(f"OK - dodano kolumnę {table}.fx_fee_pct (domyślnie 0.0015 = 0.15%).")

    conn.close()


if __name__ == "__main__":
    main()
