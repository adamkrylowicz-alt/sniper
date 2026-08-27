"""
migrate_add_cfd_crypto_value.py
=================================
Jednorazowa migracja: dokłada kolumnę cfd_crypto_value na user_settings -
appka śledzi WYŁĄCZNIE portfel Invest przez T212 API (brak dostępu do
CFD/Crypto, osobne produkty T212), więc Adam ręcznie podaje ile te konta
są dziś warte, appka dolicza to do "Total account"/"Realny zysk" (2026-08-27,
Adam: "cfd i crypto bede podawal recznie a ty bedziesz doliczal").

Domyślnie 0.00 dla wszystkich (brak podanej wartości - żeby nic się nie
zmieniło dopóki user świadomie nie wpisze kwoty w UI).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_cfd_crypto_value.py
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

    if "cfd_crypto_value" not in cols:
        cur.execute("ALTER TABLE user_settings ADD COLUMN cfd_crypto_value NUMERIC(12, 2) NOT NULL DEFAULT 0")
        conn.commit()
        print("OK - dodano kolumnę user_settings.cfd_crypto_value (default 0).")
    else:
        print("user_settings.cfd_crypto_value już istnieje - nic do zrobienia.")

    conn.close()


if __name__ == "__main__":
    main()
