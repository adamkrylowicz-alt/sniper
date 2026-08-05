"""
migrate_add_account_baseline.py
=================================
Jednorazowa migracja: dokłada kolumny account_baseline_equity/
account_baseline_at na user_settings - punkt odniesienia dla "całości konta"
(gotówka+pozycje vs start) na zakładce Aktywa (Adam, 2026-08-05: "to demo
bylo na start 5keuro... ile jest teraz i czy to zysk czy strata i w %").

Domyślny seed: 5000.00 EUR, data = teraz (nie znamy dokładnej historycznej
daty startu demo, tylko wartość którą podał Adam - patrz CLAUDE.md).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_account_baseline.py
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"
DEFAULT_BASELINE = 5000.00


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cols = [row[1] for row in cur.execute("PRAGMA table_info(user_settings)")]

    if "account_baseline_equity" not in cols:
        cur.execute("ALTER TABLE user_settings ADD COLUMN account_baseline_equity NUMERIC(12, 2)")
        conn.commit()
        print("OK - dodano kolumnę user_settings.account_baseline_equity.")
    else:
        print("user_settings.account_baseline_equity już istnieje - nic do zrobienia.")

    if "account_baseline_at" not in cols:
        cur.execute("ALTER TABLE user_settings ADD COLUMN account_baseline_at DATETIME")
        conn.commit()
        print("OK - dodano kolumnę user_settings.account_baseline_at.")
    else:
        print("user_settings.account_baseline_at już istnieje - nic do zrobienia.")

    seeded = cur.execute(
        "SELECT id FROM user_settings WHERE account_baseline_equity IS NULL"
    ).fetchall()
    if seeded:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            "UPDATE user_settings SET account_baseline_equity = ?, account_baseline_at = ? "
            "WHERE account_baseline_equity IS NULL",
            (DEFAULT_BASELINE, now),
        )
        conn.commit()
        print(f"OK - zseedowano {len(seeded)} wiersz(y) baseline={DEFAULT_BASELINE}€ (data={now} UTC).")

    conn.close()


if __name__ == "__main__":
    main()
