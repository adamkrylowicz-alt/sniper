"""
app/services/sector_diversity.py
===================================
Filtr koncentracji sektorowej (Adam, 2026-08-10 - pomysł #4 z listy
usprawnień). `max_concurrent_positions` limituje LICZBĘ otwartych pozycji,
ale nic nie stoi na przeszkodzie żeby wszystkie trafiły w ten sam sektor -
znalezione na żywo tego samego wieczoru: /status pokazał Sygnał z 6
otwartymi pozycjami, z czego 4 to spółki półprzewodnikowe/technologiczne
(ASML x2, ASM International, Nvidia) - czysty przypadek kolejności sygnałów
w czasie, nie świadoma decyzja.

Skanuje WSZYSTKIE 3 silniki naraz (w odróżnieniu od market_hours.py::
held_by_other_engine, który wyklucza silnik wołający - tu CELOWO nie
wykluczamy, bo koncentracja w obrębie JEDNEGO silnika to dokładnie ten
przypadek co wyżej) - jeden wspólny portfel na koncie T212, ryzyko sektorowe
jest realne niezależnie który silnik trzyma pozycję.

Ticker bez wpisu w sector_map.py (fail-open) NIGDY nie blokuje - ani jako
kandydat (brak sektora = nic do porównania), ani jako "już otwarta pozycja"
blokująca innych (brak sektora = nie wpada do żadnej grupy).
"""

from __future__ import annotations

from .sector_map import get_sector


def held_sector_ticker(user_id: int, environment: str, ticker: str) -> str | None:
    """
    Zwraca ticker INNEJ już otwartej pozycji (dowolny z 3 silników) w tym
    samym sektorze co `ticker`, albo None gdy brak kolizji / sektor
    nieznany. Import modeli leniwy - ten sam wzorzec co market_hours.py.
    """
    sector = get_sector(ticker)
    if sector is None:
        return None

    from ..models import ActiveTrade, EODTrade, SignalTrade

    for model in (ActiveTrade, SignalTrade, EODTrade):
        for trade in model.query.filter_by(user_id=user_id, status="OPEN", environment=environment).all():
            if trade.ticker != ticker and get_sector(trade.ticker) == sector:
                return trade.ticker
    return None
