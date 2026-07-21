"""
migrate_add_active_trade_close_price.py
=========================================
Jednorazowa migracja: dokłada kolumny active_trades.stop_target_price i
active_trades.close_price na istniejącej bazie (instance/sniper.db) - patrz
models.py::ActiveTrade i services/bot_engine.py::_manage_trailing_exit/
_finalize_closed_trade.

Cel: dało się liczyć zrealizowany zysk/strata (close_price - buy_price) bez
grzebania w dzienniku (który można wyczyścić przyciskiem "Wyczyść log") ani
bez ponownego pytania T212 o historię zleceń. stop_target_price to cena, na
którą aktualnie ustawiony jest stop_order_id (aktualizowana przy każdym
uzbrojeniu/przesunięciu); close_price to jej kopia z chwili zamknięcia -
PRZYBLIŻENIE (Market Order po przebiciu stopu może wykonać się z niewielkim
poślizgiem), nie gwarancja co do grosza.

Backfill dla wierszy sprzed tej migracji: oba pola NULL - nie da się
odtworzyć wstecz bez dostępu do historii zleceń T212 (wymaga hasła usera).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_active_trade_close_price.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]

    added = []
    if "stop_target_price" not in cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN stop_target_price NUMERIC(12, 4)")
        added.append("stop_target_price")
    if "close_price" not in cols:
        cur.execute("ALTER TABLE active_trades ADD COLUMN close_price NUMERIC(12, 4)")
        added.append("close_price")

    if not added:
        print("stop_target_price/close_price już istnieją w active_trades - nic do zrobienia.")
        conn.close()
        return

    conn.commit()
    conn.close()
    print(f"OK - dodano kolumny: {', '.join(added)}.")


if __name__ == "__main__":
    main()
