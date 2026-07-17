"""
extensions.py
=============
Instancje rozszerzeń Flaska tworzone TUTAJ (nie w __init__.py), żeby
uniknąć circular imports: models.py importuje `db` stąd, a app/__init__.py
importuje zarówno extensions, jak i models, i dopiero potem woła init_app().
"""

from apscheduler.schedulers.background import BackgroundScheduler
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO

db = SQLAlchemy()
socketio = SocketIO()

# Pętla Micro-Grid Bota (services/bot_engine.py::tick) - jeden proces,
# jeden scheduler. Start w app/__init__.py::create_app(), z ochroną przed
# podwójnym startem (Flask debug reloader woła create_app() dwa razy).
scheduler = BackgroundScheduler()

# Singleton klienta Finnhub - inicjalizowany w create_app() gdy klucz API jest dostępny.
# None gdy brak klucza (FINNHUB_API_KEY nie ustawiony w .env) - wszystkie endpointy
# korzystające z Finnhub sprawdzają czy to None i zwracają graceful fallback.
finnhub: "FinnhubClient | None" = None  # type: ignore[name-defined]
