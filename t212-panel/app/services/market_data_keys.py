"""
app/services/market_data_keys.py
==================================
Odczyt/deszyfrowanie kluczy do źródeł danych rynkowych (Finnhub/Alpaca) +
adresu własnej bramki IBKR, PER USER (10.08.2026, Adam: "kazdy user ma miec
swoje klucze... nie moze korzystac z moich") - patrz models.py::
MarketDataKeySet. Ten sam wzorzec co api_keys.py::get_decrypted_credentials
(T212), tylko jeden wiersz per user zamiast per (user, environment) - te
klucze nie mają rozróżnienia demo/live.

Wołane BEZPOŚREDNIO przy każdym call site (routes i tick() silników) -
świadomie bez współdzielonego cache per-request/per-tick: mniejszy diff i
mniejsze ryzyko regresji w kodzie zarządzającym prawdziwymi pieniędzmi niż
przepinanie sygnatur kilkunastu funkcji silników, koszt (SELECT+decrypt,
czasem kilka razy w jednym ticku) jest pomijalny.
"""
from __future__ import annotations

import json

from .. import cipher
from ..models import MarketDataKeySet

_EMPTY: dict = {
    "finnhub_api_key": None,
    "alpaca_api_key": None,
    "alpaca_api_secret": None,
    "ibkr_host": None,
    "ibkr_port": None,
}


def get_decrypted_market_data_keys(user_id: int, master_key: bytes | None) -> dict:
    """
    Zwraca ZAWSZE dict (nigdy None) - brakujący wiersz / master_key=None /
    puste pola dają same None, co price_feed.py już bezpiecznie obsługuje
    (spada na Yahoo Finance - jedyne źródło bez klucza), patrz docstring
    MarketDataKeySet. Wywołujący może więc zawsze robić
    market_keys.get("finnhub_api_key") bez sprawdzania czy dict istnieje.
    """
    if master_key is None:
        return dict(_EMPTY)

    entry = MarketDataKeySet.query.filter_by(user_id=user_id).first()
    if entry is None:
        return dict(_EMPTY)

    finnhub_key = None
    if entry.encrypted_finnhub_key:
        finnhub_key = cipher.decrypt_secret(entry.encrypted_finnhub_key, master_key)

    alpaca_key = None
    alpaca_secret = None
    if entry.encrypted_alpaca:
        alpaca = json.loads(cipher.decrypt_secret(entry.encrypted_alpaca, master_key))
        alpaca_key = alpaca.get("api_key")
        alpaca_secret = alpaca.get("api_secret")

    return {
        "finnhub_api_key": finnhub_key,
        "alpaca_api_key": alpaca_key,
        "alpaca_api_secret": alpaca_secret,
        "ibkr_host": entry.ibkr_host,
        "ibkr_port": entry.ibkr_port,
    }
