"""
app/__init__.py
================
Fabryka aplikacji (application factory pattern). Trzyma się z dala od
tworzenia instancji Flask na poziomie modułu - dzięki temu testy mogą
tworzyć wiele niezależnych instancji appki (np. z inną bazą testową),
a `db`/`socketio` (z extensions.py) nie mają circular imports z models.py.
"""

from __future__ import annotations

import os

from flask import Flask

from .config import Config
from .extensions import db, scheduler, socketio


def create_app(config_object: type = Config) -> Flask:
    Config.validate()

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    # Flask sam nie tworzy folderu instance/ (tam trzyma się plik SQLite) -
    # trzeba to zrobić ręcznie, inaczej pierwszy zapis do bazy wywali błąd.
    os.makedirs(app.instance_path, exist_ok=True)

    # WAŻNE: import modeli MUSI nastąpić PRZED db.init_app()/create_all(),
    # inaczej SQLAlchemy nie będzie wiedziało jakie tabele istnieją.
    from . import models  # noqa: F401  (import dla efektu ubocznego rejestracji modeli)

    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*")

    finnhub_key = app.config.get("FINNHUB_API_KEY")
    if finnhub_key:
        from .services.finnhub_client import FinnhubClient
        import app.extensions as _ext
        _ext.finnhub = FinnhubClient(finnhub_key)
    else:
        import logging
        logging.getLogger(__name__).warning(
            "FINNHUB_API_KEY nie ustawiony w .env - ceny live i wykresy niedostepne."
        )

    with app.app_context():
        db.create_all()

    _register_blueprints(app)
    _register_context_processors(app)
    _register_scheduler(app)
    _register_root_redirect(app)

    return app


def _register_root_redirect(app: Flask) -> None:
    """
    Goły "/" nie ma własnego widoku (każdy blueprint ma swój url_prefix -
    /warp, /settings, /pie, /bot, /keys) - bez tego Flask zwracał 404 przy
    wejściu na sam adres appki. Przekierowuje do Warp Mode gdy zalogowany,
    inaczej do ekranu logowania.
    """
    from flask import redirect, request, url_for

    from .services.session_store import SESSION_COOKIE_NAME, get_session

    @app.route("/")
    def root():
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if get_session(token) is not None:
            return redirect(url_for("scalping.warp_view"))
        return redirect(url_for("auth.login_view"))


def _register_scheduler(app: Flask) -> None:
    """
    Pętla Micro-Grid Bota (services/bot_engine.py::tick), wołana co 60s.

    OCHRONA PRZED PODWÓJNYM STARTEM: run.py woła app.run(..., debug=True) na
    SZTYWNO, a Werkzeug z debug=True zawsze odpala reloader - PROCES-RODZIC
    (watcher plików) i PROCES-DZIECKO (faktyczny serwer) OBA wykonują
    create_app() od zera, jako dwa OSOBNE procesy OS. Bez tej ochrony
    scheduler startowałby w OBU - dwa niezależne tick() co 60s, dublujące
    BotAuditLog (a docelowo, w kolejnej części, dublujące PRAWDZIWE zlecenia
    bota - dokładnie ten sam rodzaj kolizji, jaki już raz złapaliśmy przy
    bulk-fetchu logo, patrz services/logo_cache.py).

    UWAGA: sprawdzamy WYŁĄCZNIE zmienną środowiskową WERKZEUG_RUN_MAIN, NIE
    app.debug - `app.debug` NIE jest jeszcze poprawnie ustawione na tym
    etapie (run.py przekazuje debug=True dopiero do app.run(), już PO tym
    jak create_app() zwróci apkę; w .env FLASK_DEBUG=false, więc app.debug
    tutaj i tak pokazywałby False, myląco). Werkzeug ustawia
    WERKZEUG_RUN_MAIN=true TYLKO w procesie-dziecku, który faktycznie
    obsługuje requesty - jego BRAK oznacza tu proces-watcher (nie startuj)
    ALBO uruchomienie poza app.run() w ogóle (skrypty jednorazowe typu
    migrate_*.py/diagnose_*.py - też słusznie nie startujemy im schedulera).
    To założenie trzyma się dopóki run.py ma debug=True na sztywno - jeśli
    to się kiedyś zmieni (realny prod bez reloadera), ten warunek trzeba
    zrewidować.
    """
    if scheduler.running:
        return
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    from .services import bot_engine

    scheduler.add_job(
        func=lambda: bot_engine.tick(app),
        trigger="interval", seconds=60, id="bot_tick", replace_existing=True,
    )
    scheduler.start()


def _register_context_processors(app: Flask) -> None:
    """
    Udostępnia is_authenticated w KAŻDYM szablonie, bez konieczności
    ręcznego przekazywania tego z każdego widoku - używane w base.html
    do pokazania/ukrycia paska nawigacji (Wyloguj / Klucze API).

    UWAGA: to tylko odczyt (bool), NIE robi login_required - widoki nadal
    same chronią się dekoratorem gdzie trzeba.
    """
    from .services.session_store import SESSION_COOKIE_NAME, get_session

    @app.context_processor
    def inject_auth_state():
        from flask import request
        token = request.cookies.get(SESSION_COOKIE_NAME)
        session_data = get_session(token)

        theme = "dark"  # domyślny motyw dla niezalogowanych / braku ustawień
        if session_data is not None:
            from .models import UserSettings
            settings = UserSettings.query.filter_by(user_id=session_data.user_id).first()
            if settings is not None and settings.dark_mode is False:
                theme = "light"

        return {"is_authenticated": session_data is not None, "current_theme": theme}


def _register_blueprints(app: Flask) -> None:
    """
    Rejestracja blueprintów - na razie pusto (routes/*.py jeszcze nie
    istnieją), ale zostawiam gotowy hak, żeby kolejny krok (auth.py,
    dashboard.py, scalping.py, settings.py) był tylko dopisaniem 4 linii
    tutaj, bez ruszania reszty fabryki.
    """
    from .routes.auth import auth_bp
    from .routes.scalping import scalping_bp
    from .routes.api_keys import api_keys_bp
    from .routes.settings import settings_bp
    from .routes.report import report_bp
    from .routes.pie import pie_bp
    from .routes.bot import bot_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(scalping_bp)
    app.register_blueprint(api_keys_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(report_bp)
    app.register_blueprint(pie_bp)
    app.register_blueprint(bot_bp)
