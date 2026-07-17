"""
app/services/instrument_cache.py
==================================
Zarządzanie lokalnym cache'em instrumentów T212 (patrz models.Instrument
po wyjaśnienie DLACZEGO to jest cache, a nie żywe zapytanie).

KATEGORIE (zakładki Akcje/ETF-y/ETP-y z dźwignią/Warranty, wzorowane na
realnej kategoryzacji T212 - Ordinary/Preferred shares, ETF, ETP, ETC, REIT,
Investment Trust): publiczne API T212 nie daje tej klasyfikacji wprost,
zwraca tylko uproszczone "type" (STOCK/ETF/WARRANT). Dźwignię wykrywamy
sami z nazwy (patrz _LEVERAGE_PATTERN) - zweryfikowane na prawdziwych danych
(15808 zsynchronizowanych instrumentów): 924/6442 ETF-ów pasuje, zero
fałszywych trafień wśród ETF-ów, jeden potencjalny fałszywy alarm w całej
bazie ("10X Genomics", spółka STOCK) - dlatego wzorzec stosujemy WYŁĄCZNIE
do typu ETF, nigdy do STOCK.
"""

from __future__ import annotations

import datetime as dt
import re

from ..extensions import db
from ..models import Instrument, InstrumentCacheMeta

MIN_REFRESH_INTERVAL = dt.timedelta(hours=1)

# "3x", "-5x", "2X" itp. - dopasowane tylko w obrębie ETF (patrz docstring modułu).
_LEVERAGE_PATTERN = re.compile(r"-?\d+x\b", re.IGNORECASE)

CATEGORIES = ("stock", "etf", "leveraged", "warrant")


class RefreshTooSoonError(Exception):
    """Podniesione gdy ktoś próbuje odświeżyć cache za wcześnie po poprzednim razie."""
    def __init__(self, retry_after: dt.timedelta):
        self.retry_after = retry_after
        super().__init__(f"Za wcześnie na kolejne odświeżenie - poczekaj jeszcze {retry_after}.")


def _detect_leveraged(instrument_type: str | None, name: str) -> bool:
    if instrument_type != "ETF":
        return False
    return bool(_LEVERAGE_PATTERN.search(name))


def get_last_synced_at() -> dt.datetime | None:
    meta = InstrumentCacheMeta.query.get(1)
    return meta.last_synced_at if meta else None


def refresh_instrument_cache(client, force: bool = False) -> int:
    """
    Pobiera pełną listę instrumentów z T212 (client.get_instruments()) i
    nadpisuje nią lokalny cache. Wymusza minimalny odstęp MIN_REFRESH_INTERVAL
    od poprzedniego odświeżenia, żeby nie zjeść rate limitu przez pomyłkę
    (np. dwa razy kliknięte "Odśwież" pod rząd).

    Zwraca liczbę zapisanych instrumentów.
    Rzuca RefreshTooSoonError jeśli odstęp jeszcze nie minął (force=True pomija tę blokadę -
    używać świadomie, np. do jednorazowej ręcznej interwencji administracyjnej).
    """
    meta = InstrumentCacheMeta.query.get(1)
    now = dt.datetime.utcnow()

    if meta and meta.last_synced_at and not force:
        elapsed = now - meta.last_synced_at
        if elapsed < MIN_REFRESH_INTERVAL:
            raise RefreshTooSoonError(MIN_REFRESH_INTERVAL - elapsed)

    instruments = client.get_instruments()

    # Prosty "wipe and reload" - lista instrumentów zmienia się rzadko,
    # a to znacznie prostsze niż wyliczanie diffów. Przy ~kilkunastu tysiącach
    # wierszy to nadal ułamek sekundy w SQLite.
    Instrument.query.delete()
    for item in instruments:
        ticker = item.get("ticker")
        if not ticker:
            continue
        name = item.get("name") or ticker
        instrument_type = item.get("type")
        db.session.add(Instrument(
            ticker=ticker,
            name=name,
            instrument_type=instrument_type,
            currency_code=item.get("currencyCode"),
            is_leveraged=_detect_leveraged(instrument_type, name),
        ))

    if meta is None:
        meta = InstrumentCacheMeta(id=1, last_synced_at=now)
        db.session.add(meta)
    else:
        meta.last_synced_at = now

    db.session.commit()
    return len(instruments)


def _category_filter(category: str | None):
    """
    Zwraca warunek SQLAlchemy dla danej zakładki, albo None dla "bez filtra"
    (category=None albo nierozpoznana wartość - traktujemy jak brak filtra,
    nie błąd, żeby literówka w query stringu nie wywalała 500).
    """
    if category == "stock":
        return Instrument.instrument_type == "STOCK"
    if category == "etf":
        return db.and_(Instrument.instrument_type == "ETF", Instrument.is_leveraged.is_(False))
    if category == "leveraged":
        return db.and_(Instrument.instrument_type == "ETF", Instrument.is_leveraged.is_(True))
    if category == "warrant":
        return Instrument.instrument_type == "WARRANT"
    return None


def get_category_counts() -> dict[str, int]:
    """Liczby do plakietek przy zakładkach - 4 tanie zapytania COUNT, wołane raz na wejście na stronę."""
    return {cat: Instrument.query.filter(_category_filter(cat)).count() for cat in CATEGORIES}


def search_instruments(
    query: str, limit: int = 20, offset: int = 0, category: str | None = None
) -> list[Instrument]:
    """
    Wyszukiwanie WYŁĄCZNIE po lokalnym cache'u (błyskawiczne, bez rate limitu).
    Dopasowanie po fragmencie tickera LUB nazwy, bez rozróżniania wielkości liter.

    category: jedna z CATEGORIES - zawęża wyniki do zakładki (patrz _category_filter).
    Z PUSTYM query ORAZ category=None zwracamy [] (jak dotychczas - nie chcemy
    przypadkiem zrzucić całej bazy 15k+ wierszy bez żadnego filtra). Ale z
    WYBRANĄ zakładką pusty query oznacza "przeglądaj tę kategorię od A do Z" -
    to jest właśnie tryb "przeglądaj wszystko" opisany w watchlist_search(),
    który wcześniej nie działał (ta funkcja zawsze zwracała [] dla pustego query,
    niezależnie od kategorii).

    offset: do paginacji "Załaduj więcej".
    """
    query = (query or "").strip()
    cat_filter = _category_filter(category)

    if not query and cat_filter is None:
        return []

    stmt = Instrument.query
    if cat_filter is not None:
        stmt = stmt.filter(cat_filter)
    if query:
        pattern = f"%{query}%"
        stmt = stmt.filter(db.or_(Instrument.ticker.ilike(pattern), Instrument.name.ilike(pattern)))

    return stmt.order_by(Instrument.ticker).offset(offset).limit(limit).all()
