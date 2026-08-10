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
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import db, scheduler, socketio


def create_app(config_object: type = Config) -> Flask:
    Config.validate()

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    # Appka moze byc dostepna albo bezposrednio w LAN (http://192.168.1.50:5050),
    # albo przez nginx-proxy-manager z HTTPS na zewnatrz (sniper.duckdns.org).
    # ProxyFix sprawia, ze request.is_secure/request.remote_addr poprawnie
    # odzwierciedlaja PRAWDZIWEGO klienta (przez naglowki X-Forwarded-*), a nie
    # NPM jako "klienta" - potrzebne m.in. do dynamicznej flagi Secure na
    # ciasteczku sesji (auth.py) i do throttlingu logowania po prawdziwym IP.
    # x_for/x_proto/x_host=1 - ufamy DOKLADNIE JEDNEMU skokowi proxy (NPM),
    # bo appka nie stoi za zadnym innym posrednikiem.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Flask sam nie tworzy folderu instance/ (tam trzyma się plik SQLite) -
    # trzeba to zrobić ręcznie, inaczej pierwszy zapis do bazy wywali błąd.
    os.makedirs(app.instance_path, exist_ok=True)

    # WAŻNE: import modeli MUSI nastąpić PRZED db.init_app()/create_all(),
    # inaczej SQLAlchemy nie będzie wiedziało jakie tabele istnieją.
    from . import models  # noqa: F401  (import dla efektu ubocznego rejestracji modeli)

    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*")

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

    OCHRONA PRZED PODWÓJNYM STARTEM: gdy run.py woła app.run(..., debug=True)
    (lokalny dev), Werkzeug zawsze odpala reloader - PROCES-RODZIC (watcher
    plików) i PROCES-DZIECKO (faktyczny serwer) OBA wykonują create_app() od
    zera, jako dwa OSOBNE procesy OS. Bez tej ochrony scheduler startowałby w
    OBU - dwa niezależne tick() co 60s, dublujące BotAuditLog (a docelowo, w
    kolejnej części, dublujące PRAWDZIWE zlecenia bota - dokładnie ten sam
    rodzaj kolizji, jaki już raz złapaliśmy przy bulk-fetchu logo, patrz
    services/logo_cache.py). Werkzeug ustawia WERKZEUG_RUN_MAIN=true TYLKO w
    procesie-dziecku, który faktycznie obsługuje requesty - jego BRAK w tym
    trybie oznacza proces-watcher (nie startuj).

    UWAGA (bug naprawiony 2026-07-20): ten warunek reloadera ma sens WYŁĄCZNIE
    w trybie debug. Produkcyjnie run.py NIE woła w ogóle app.run(debug=True) -
    leci przez waitress.serve() (patrz run.py), który nie ma reloadera i nigdy
    nie ustawia WERKZEUG_RUN_MAIN. Sprawdzanie tej zmiennej bezwarunkowo
    (niezależnie od Config.DEBUG) powodowało, że scheduler NIGDY się nie
    uruchamiał produkcyjnie - bot dawał się "aktywować" (reconcile działał),
    ale tick() (właściwe wejścia w pozycje) nigdy nie był wołany. Stąd warunek
    niżej ogranicza sprawdzanie WERKZEUG_RUN_MAIN tylko do przypadku
    Config.DEBUG=True; poza trybem debug (waitress, jeden proces) scheduler
    startuje zawsze.
    """
    if scheduler.running:
        return
    if Config.DEBUG and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    from .services import bot_credentials, bot_engine, daily_summary, eod_engine, signal_engine, telegram_commands, weekend_guard

    # Autostart TYMCZASOWY "DO ODWOŁANIA" (Adam, 2026-07-27) - patrz pelny
    # docstring w services/bot_credentials.py. Odtwarza poswiadczenia
    # zapisane przy ostatniej recznej aktywacji (dowolnego z trzech botow),
    # bez ponownego pytania o haslo, i od razu wola reconcile() KAZDEGO z
    # trzech silnikow dla kazdego odtworzonego usera - dokladnie ten sam
    # efekt co reczna aktywacja w UI (kazdy reconcile() sam sprawdza swoja
    # WLASNA flage is_bot_active/is_active w bazie i nic nie robi dla
    # silnika ktory faktycznie nie byl wlaczony, wiec bezpieczne wolac
    # wszystkie trzy niezaleznie od tego, ktore konkretnie user mial aktywne).
    restored_user_ids = bot_credentials.load_autostart(app.instance_path)
    if restored_user_ids:
        import logging
        logging.getLogger(__name__).info(
            f"Autostart bota: odtworzono poswiadczenia (bez hasla) dla user_id={restored_user_ids}"
        )
        with app.app_context():
            for uid in restored_user_ids:
                bot_engine.reconcile(uid)
                signal_engine.reconcile(uid)
                eod_engine.reconcile(uid)

    scheduler.add_job(
        func=lambda: bot_engine.tick(app),
        trigger="interval", seconds=60, id="bot_tick", replace_existing=True,
    )
    # Strategia sygnałowa RSI/MA/ATR (services/signal_engine.py) - osobny job,
    # osobny silnik, ten sam interwał co Micro-Grid (decyzja Adama 2026-07-24,
    # patrz docs/IDEAS_v2.md: "osobna strategia", nie rozszerzenie bot_tick).
    scheduler.add_job(
        func=lambda: signal_engine.tick(app),
        trigger="interval", seconds=60, id="signal_tick", replace_existing=True,
    )
    # Modul EOD (services/eod_engine.py) - TRZECI osobny job, PRD: "co 1
    # minute sprawdza swiece 1-minutowe" - sam tick() gate'uje sie na
    # EOD_WINDOW (16:00-17:30 Amsterdam), wiec poza tym oknem to tani no-op.
    scheduler.add_job(
        func=lambda: eod_engine.tick(app),
        trigger="interval", seconds=60, id="eod_tick", replace_existing=True,
    )
    # Dzienny raport zysk/strata mailem (ustalone z Adamem 2026-07-21) - 22:01
    # czasu Amsterdamu (timezone="Europe/Amsterdam", APScheduler sam ogarnia
    # przejście CEST/CET, nie trzeba przeliczać na UTC ręcznie), tuż po
    # zamknięciu sesji USA (~22:00 CEST latem) - łapie cały dzień handlu.
    scheduler.add_job(
        func=lambda: bot_engine.daily_report(app),
        trigger="cron", hour=22, minute=1, timezone="Europe/Amsterdam",
        id="bot_daily_report", replace_existing=True,
    )
    # Podsumowanie P&L WSZYSTKICH 3 silników na Telegram, dwa razy dziennie
    # (Adam, 2026-08-05: "dzienne podsumowanie P&L wieczorem i rano") - patrz
    # services/daily_summary.py, celowo OSOBNE od bot_daily_report wyżej
    # (ten mailowy dotyczy wyłącznie Micro-Gridu). Rano 07:45 (przed
    # otwarciem Euronext 9:00 - łapie co się działo w nocy na USA/24-godzinnych
    # tickerach), wieczorem 22:05 (tuż po bot_daily_report, ~zamknięcie sesji USA).
    scheduler.add_job(
        func=lambda: daily_summary.send_daily_summary(app, "poranny"),
        trigger="cron", hour=7, minute=45, timezone="Europe/Amsterdam",
        id="telegram_morning_summary", replace_existing=True,
    )
    scheduler.add_job(
        func=lambda: daily_summary.send_daily_summary(app, "wieczorny"),
        trigger="cron", hour=22, minute=5, timezone="Europe/Amsterdam",
        id="telegram_evening_summary", replace_existing=True,
    )
    # Podsumowania tygodniowe/miesięczne (Adam, 2026-08-09) - ten sam
    # send_daily_summary co wyżej, tylko szersze okno (days=7/30, patrz
    # daily_summary.py::_engine_pnl_24h) i rzadszy harmonogram. Poniedziałek
    # 08:00 (podsumowuje ubiegły tydzień przed otwarciem), 1. dnia miesiąca 08:05.
    scheduler.add_job(
        func=lambda: daily_summary.send_daily_summary(app, "tygodniowy", days=7),
        trigger="cron", day_of_week="mon", hour=8, minute=0, timezone="Europe/Amsterdam",
        id="telegram_weekly_summary", replace_existing=True,
    )
    scheduler.add_job(
        func=lambda: daily_summary.send_daily_summary(app, "miesięczny", days=30),
        trigger="cron", day=1, hour=8, minute=5, timezone="Europe/Amsterdam",
        id="telegram_monthly_summary", replace_existing=True,
    )
    # Obsługa komend przychodzących z Telegrama (Adam: "dodaj /status") -
    # krótki poll co 15s (patrz docstring telegram_commands.py - appka nie
    # ma publicznego HTTPS do webhooka Telegrama, więc polling jest prostszym
    # wystarczającym rozwiązaniem przy jednym użytkowniku).
    scheduler.add_job(
        func=lambda: telegram_commands.poll_and_handle(app),
        trigger="interval", seconds=15, id="telegram_commands_poll", replace_existing=True,
    )
    # Weekendowe zawieszenie SL (Adam, 2026-08-10 - patrz services/
    # weekend_guard.py) - piątek 21:00 anuluje żywe zlecenia STOP dla
    # wszystkich 3 silników (luka na otwarciu USA po weekendzie mogłaby
    # wyciąć pozycję po najgorszej cenie na chwilowym squeeze'u), poniedziałek
    # 11:00 przywraca je z powrotem. Manualny override: /slpause i /slresume
    # na Telegramie (telegram_commands.py) wołają te same dwie funkcje.
    scheduler.add_job(
        func=lambda: weekend_guard.suspend_all(app),
        trigger="cron", day_of_week="fri", hour=21, minute=0, timezone="Europe/Amsterdam",
        id="weekend_sl_suspend", replace_existing=True,
    )
    scheduler.add_job(
        func=lambda: weekend_guard.restore_all(app),
        trigger="cron", day_of_week="mon", hour=11, minute=0, timezone="Europe/Amsterdam",
        id="weekend_sl_restore", replace_existing=True,
    )
    # DOGONIENIE przy starcie (dodane 2026-08-10, znalezione na żywo - Adam
    # zauważył że SL były nadal aktywne mimo bycia w oknie, bo appka
    # wystartowała z tym kodem DOPIERO PO tym jak cron "piątek 21:00" już
    # minął w tym tygodniu - job cyklu nie odpala się retroaktywnie, tylko
    # dokładnie w zaplanowanym momencie) - jeśli restart/start appki
    # wypada JUŻ W TRAKCIE okna (np. wdrożenie w sobotę/niedzielę/
    # poniedziałek przed 11:00), zawieś od razu zamiast czekać do
    # następnego piątku. suspend_all() jest idempotentne (filtruje
    # sl_suspended_for_weekend=False), bezpieczne wołać nawet gdy część
    # pozycji już zawieszona wcześniej.
    if weekend_guard.in_suspension_window():
        weekend_guard.suspend_all(app)
    scheduler.start()


def _register_context_processors(app: Flask) -> None:
    """
    Udostępnia is_authenticated w KAŻDYM szablonie, bez konieczności
    ręcznego przekazywania tego z każdego widoku - używane w base.html
    do pokazania/ukrycia paska nawigacji (Wyloguj / Klucze API).

    UWAGA: to tylko odczyt (bool), NIE robi login_required - widoki nadal
    same chronią się dekoratorem gdzie trzeba.
    """
    from .i18n import DEFAULT_LANGUAGE, LANG_COOKIE_NAME, SUPPORTED_LANGUAGES, all_translations
    from .i18n import t as translate
    from .services.session_store import SESSION_COOKIE_NAME, get_session

    @app.context_processor
    def inject_auth_state():
        from flask import request
        token = request.cookies.get(SESSION_COOKIE_NAME)
        session_data = get_session(token)

        theme = "dark"  # domyślny motyw dla niezalogowanych / braku ustawień
        active_environment = "demo"  # ten sam bezpieczny default co UserSettings.active_environment
        # Język: ciasteczko (ustawiane przez /lang/toggle) wygrywa zawsze gdy
        # obecne - działa też PRZED zalogowaniem (np. ekran logowania dla
        # obcokrajowca). Bez ciasteczka - język z UserSettings, a dla
        # niezalogowanych/nowych userów domyślny polski.
        lang = request.cookies.get(LANG_COOKIE_NAME)
        if lang not in SUPPORTED_LANGUAGES:
            lang = None
        # Liczniki otwartych pozycji per silnik - Adam 2026-07-28: "w
        # zakladkach pododawaj liczbe porzadkowa zeby bylo latwo widziec ile
        # pozycji jest otwartych" - pokazywane jako plakietka przy Bot/
        # Sygnał/EOD w topbarze (base.html), zeby nie trzeba bylo wchodzic
        # do kazdej zakladki osobno. Tylko lokalny COUNT w bazie, ZERO
        # zapytan do T212 - bezpieczne nawet przy ciasnym rate limicie demo.
        open_position_counts = {"bot": 0, "signal": 0, "eod": 0, "aktywa": 0}
        is_admin = False
        if session_data is not None:
            from .models import ActiveTrade, EODTrade, SignalTrade, User, UserSettings
            user = User.query.get(session_data.user_id)
            is_admin = bool(user is not None and user.is_admin)
            settings = UserSettings.query.filter_by(user_id=session_data.user_id).first()
            if settings is not None and settings.dark_mode is False:
                theme = "light"
            if settings is not None:
                active_environment = settings.active_environment
                if lang is None:
                    lang = settings.language

            # "Aktywa" (scalping.py::portfolio_view) - WSZYSTKIE pozycje z
            # T212 (nie tylko botowe), stad nie liczba z lokalnej bazy jak
            # wyzej, tylko len() z _portfolio_cache - TEGO SAMEGO cache co
            # sama strona uzywa do renderu bez live-calla (patrz docstring
            # portfolio_view - swiadomie zero zapytan do T212 przy
            # renderowaniu, wask rate limit demo). Brak cache (jeszcze nikt
            # nie odwiedzil Aktywa w tej sesji procesu) - brak plakietki,
            # nie zgadujemy.
            from .routes.scalping import _portfolio_cache
            cached_portfolio = _portfolio_cache.get(session_data.user_id)

            open_position_counts = {
                "bot": ActiveTrade.query.filter_by(user_id=session_data.user_id, status="OPEN", environment=active_environment).count(),
                "signal": SignalTrade.query.filter_by(user_id=session_data.user_id, status="OPEN", environment=active_environment).count(),
                "eod": EODTrade.query.filter_by(user_id=session_data.user_id, status="OPEN", environment=active_environment).count(),
                "aktywa": len(cached_portfolio["positions"]) if cached_portfolio else 0,
            }

        if lang is None:
            lang = DEFAULT_LANGUAGE

        return {
            "is_authenticated": session_data is not None,
            "is_admin": is_admin,
            "current_theme": theme,
            "open_position_counts": open_position_counts,
            "active_environment": active_environment,
            "current_lang": lang,
            "t": lambda text: translate(text, lang),
            "i18n_json": all_translations(lang),
        }


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
    from .routes.instrument import instrument_bp
    from .routes.signal import signal_bp
    from .routes.eod import eod_bp
    from .routes.i18n import i18n_bp
    from .routes.help import help_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(scalping_bp)
    app.register_blueprint(api_keys_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(report_bp)
    app.register_blueprint(pie_bp)
    app.register_blueprint(bot_bp)
    app.register_blueprint(instrument_bp)
    app.register_blueprint(signal_bp)
    app.register_blueprint(eod_bp)
    app.register_blueprint(i18n_bp)
    app.register_blueprint(help_bp)
