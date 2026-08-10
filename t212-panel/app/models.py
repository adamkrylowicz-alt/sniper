"""
models.py
=========
Schemat bazy danych SNIPER (SQLite + SQLAlchemy).

Ten plik NIE zawiera logiki szyfrowania - tylko strukturę tabel.
Właściwe szyfrowanie/odszyfrowanie odbywa się w warstwie serwisowej
(services/*.py), korzystającej z cipher.py. Tutaj przechowujemy
wyłącznie już zaszyfrowane blob-y (bytes) + sole.

Import w app/__init__.py:
    from .extensions import db
    from . import models  # rejestruje modele w metadata przed db.create_all()
"""

from __future__ import annotations

import datetime as dt

from .extensions import db


class User(db.Model):
    """
    Konto użytkownika platformy.

    Zero-Knowledge - key wrapping:
    -------------------------------
    password_hash to hash do LOGOWANIA (werkzeug.security), zupełnie
    OSOBNY mechanizm od szyfrowania sekretów.

    Do szyfrowania sekretów (kluczy API T212) używamy jednego, per-user
    master_key (patrz cipher.py). master_key NIGDY nie jest zapisywany
    jawnie - trzymamy go dwukrotnie "opakowany" (wrapped):
      - wrapped_master_key_by_password -> odszyfrowywalny hasłem usera
      - wrapped_master_key_by_recovery -> odszyfrowywalny recovery-kodem

    Admin nie zna ani hasła, ani recovery code -> nie ma dostępu do niczego.
    Recovery code jest pokazywany UŻYTKOWNIKOWI TYLKO RAZ, przy rejestracji -
    my (serwer) nigdy go nie przechowujemy jawnie, więc nie da się go
    "przypomnieć" - tylko wygenerować nowy (co wymaga ponownego zawinięcia
    master_key, patrz routes/auth.py:regenerate_recovery_code).
    """
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    wrapped_master_key_by_password = db.Column(db.LargeBinary, nullable=False)
    salt_password = db.Column(db.LargeBinary, nullable=False)

    wrapped_master_key_by_recovery = db.Column(db.LargeBinary, nullable=False)
    salt_recovery = db.Column(db.LargeBinary, nullable=False)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    # -- Aktywacja konta (od 19.07.2026, odkad rejestracja jest publiczna) ----
    # is_admin: PIERWSZY kiedykolwiek zarejestrowany user (patrz auth.py) -
    # tylko on widzi panel zatwierdzania w Ustawieniach.
    # email_verified: klikniecie w link z maila aktywacyjnego (dowod ze
    # username/email jest prawdziwy, NIE wystarcza do zalogowania).
    # is_active: recznie zatwierdzone przez admina w panelu - DOPIERO to
    # odblokowuje logowanie. Dwa niezalezne gate'y celowo (klikniecie w link
    # moze zrobic bot, zatwierdzenie admina - nie).
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    activation_token = db.Column(db.String(64), nullable=True, unique=True)

    # Relacje 1:1 / 1:N
    api_keys = db.relationship(
        "ApiKeySet", back_populates="user", cascade="all, delete-orphan"
    )
    settings = db.relationship(
        "UserSettings", back_populates="user", uselist=False,
        cascade="all, delete-orphan",
    )
    orders = db.relationship(
        "OrderLog", back_populates="user", cascade="all, delete-orphan"
    )
    pies = db.relationship(
        "Pie", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - czysto diagnostyczne
        return f"<User {self.username}>"


class ApiKeySet(db.Model):
    """
    Zaszyfrowany klucz+sekret API T212 dla danego użytkownika + środowiska
    (demo/live). Szyfrowane WSPÓLNYM master_key użytkownika (patrz User) -
    ten model NIE ma już własnego key-wrappingu, tylko czysty ciphertext.

    encrypted_key: JSON {"api_key": "...", "api_secret": "..."} zaszyfrowany
    jako całość przez cipher.encrypt_secret(json_string, master_key) -
    trzymanie ich razem (nie w dwóch kolumnach) upraszcza odczyt: zawsze
    odszyfrowujesz i dostajesz komplet, bez ryzyka rozjazdu.
    """
    __tablename__ = "api_key_sets"
    __table_args__ = (
        db.UniqueConstraint("user_id", "environment", name="uq_user_environment"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    environment = db.Column(db.String(10), nullable=False)  # "demo" albo "live"

    encrypted_key = db.Column(db.LargeBinary, nullable=False)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow,
        nullable=False,
    )

    user = db.relationship("User", back_populates="api_keys")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ApiKeySet user_id={self.user_id} env={self.environment}>"


class Instrument(db.Model):
    """
    Lokalny CACHE listy instrumentów z T212 (/equity/metadata/instruments).

    DLACZEGO CACHE, NIE ŻYWE ZAPYTANIE PRZY KAŻDYM WYSZUKIWANIU:
    Endpoint get_instruments() ma bardzo wąski rate limit (zaobserwowane:
    wyczerpany już po JEDNYM wywołaniu - patrz komentarz w t212_client.py
    i historia tej rozmowy). Odpytywanie go za każdym razem gdy user wpisuje
    literkę w wyszukiwarce rozwaliłoby appkę przy pierwszym większym użyciu.

    Zamiast tego: admin/user od czasu do czasu odświeża ten cache RĘCZNIE
    (patrz routes/settings.py - refresh_instrument_cache, z wymuszonym
    minimalnym odstępem czasu między odświeżeniami), a samo wyszukiwanie
    zawsze czyta z lokalnej bazy SQLite - szybkie, bez limitu, offline-safe.
    """
    __tablename__ = "instruments"
    __table_args__ = (
        db.Index("ix_instruments_type_leveraged", "instrument_type", "is_leveraged"),
    )

    ticker = db.Column(db.String(30), primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    instrument_type = db.Column(db.String(20), nullable=True)
    currency_code = db.Column(db.String(10), nullable=True)

    # Wyliczane przy każdym refresh_instrument_cache() z nazwy (T212 nie daje
    # takiej flagi w API) - wzorzec "-?Nx" (np. "3x", "-5x"), TYLKO wśród ETF.
    # Skoping do ETF jest celowy: ta sama heurystyka zastosowana do STOCK
    # dałaby fałszywy alarm na spółce "10X Genomics" (patrz instrument_cache.py).
    is_leveraged = db.Column(db.Boolean, default=False, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Instrument {self.ticker} - {self.name}>"


class InstrumentCacheMeta(db.Model):
    """
    Jeden wiersz (singleton) trzymający czas ostatniej synchronizacji cache'u
    instrumentów - do wymuszenia minimalnego odstępu między odświeżeniami
    (patrz Instrument - powód istnienia cache'u w ogóle).
    """
    __tablename__ = "instrument_cache_meta"

    id = db.Column(db.Integer, primary_key=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)


class YahooSymbolMap(db.Model):
    """
    Cache rozwiazanych symboli Yahoo Finance dla tickerow T212, ktorych
    finnhub_client.py::t212_to_finnhub() nie potrafi zmapowac automatycznie
    (nic poza wzorcem _US_EQ i recznymi wyjatkami w TICKER_MAP - w praktyce
    wiekszosc tickerow spoza gield USA, np. europejskich). Rozwiazywane
    LENIWIE przez services/yahoo_resolver.py przy PIERWSZYM realnym uzyciu
    danego tickera (klik w aktywo, dodanie do watchlisty itp.), nie hurtowo
    dla calej bazy - i tak zapisane na stale, zeby kolejne proby byly
    natychmiastowe zamiast odpytywac (nieoficjalne) API wyszukiwania Yahoo
    za kazdym razem.

    yahoo_symbol=NULL oznacza "probowano, nie znaleziono zweryfikowanego
    dopasowania" - TEZ cache'owane (inaczej appka dobijalaby Yahoo przy
    kazdym wejsciu na strone tickera bez pokrycia).
    """
    __tablename__ = "yahoo_symbol_map"

    ticker = db.Column(db.String(30), primary_key=True)
    yahoo_symbol = db.Column(db.String(30), nullable=True)
    resolved_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)


class UserSettings(db.Model):
    """
    Ustawienia trybu Normal i Warp per użytkownik.

    max_order_value: Hard Cap - maksymalna wartość (Quantity * Price)
    dla pojedynczego zlecenia. None = brak limitu (odradzane w Warp Mode).

    warp_grid: lista tickerów przypisanych do siatki 3x3, zapisana jako
    JSON-string (SQLite nie ma natywnego typu array) - kolejność w liście
    odpowiada pozycji kafelka 0-8.

    cooldown_ms: minimalny odstęp czasu między dwoma kliknięciami w ten sam
    kafelek, jako zabezpieczenie przed przypadkowym podwójnym zleceniem.
    """
    __tablename__ = "user_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)

    max_order_value = db.Column(db.Numeric(12, 2), nullable=True)
    cooldown_ms = db.Column(db.Integer, default=400, nullable=False)

    # warp_grid: max 9 tickerów FAKTYCZNIE pokazywanych jako kafelki w Warp
    # Mode - to PODZBIÓR listy favorites, wybierany osobno.
    warp_grid = db.Column(db.Text, nullable=True)  # JSON: ["AAPL_US_EQ", ...]

    # favorites: szersza lista "co mnie interesuje" (watchlist), NIEZALEŻNA
    # od tego co akurat jest w siatce - można mieć np. 30 ulubionych, a w
    # siatce trzymać tylko 9 z nich naraz.
    favorites = db.Column(db.Text, nullable=True)  # JSON: ["AAPL_US_EQ", ...]

    default_quantity = db.Column(db.Numeric(12, 4), default=1, nullable=False)

    sound_enabled = db.Column(db.Boolean, default=True, nullable=False)
    dark_mode = db.Column(db.Boolean, default=True, nullable=False)

    focus_tiles = db.Column(db.Integer, default=1, nullable=False)  # 1-9 kafelków w Focus Mode

    # Punkt odniesienia dla "całości konta" na zakładce Aktywa (dodane
    # 2026-08-05, Adam: "to demo bylo na start 5keuro... jak i gdzie
    # wyswietlic ile jest teraz i czy to zysk czy strata") - equity CAŁEGO
    # konta T212 (gotówka+pozycje, ten sam wzorzec co bot_engine.py::
    # _get_current_equity) w chwili ustawienia punktu odniesienia, NIE
    # sztywna stała - Adam czasem RĘCZNIE resetuje konto demo (patrz
    # [[project_snajper_demo_resets]] w pamięci Claude, zero śladu w logach),
    # więc wartość musi dać się odświeżyć jednym klikiem zamiast wymagać
    # zmiany stałej w kodzie po każdym takim reset.
    account_baseline_equity = db.Column(db.Numeric(12, 2), nullable=True)
    account_baseline_at = db.Column(db.DateTime, nullable=True)

    # Przełącznik demo/live (2026-08-06, Adam: "przełącz na live... i dodaj
    # guzik przełącznik live demo") - JEDEN toggle na konto (nie per-silnik),
    # bo wszystkie 3 silniki + Warp Mode dzielą TO SAMO fizyczne konto T212
    # w danej instancji. Zastępuje dotychczasowe stałe modułowe BOT_ENVIRONMENT/
    # SIGNAL_ENVIRONMENT/EOD_ENVIRONMENT (zawsze "demo" na sztywno) -
    # bot_credentials.get_environment(user_id) czyta to pole, domyślnie
    # "demo" gdy brak wiersza UserSettings (bezpieczny default, zero zmiany
    # zachowania dla nowych userów). Stara teza "T212 nie wspiera zleceń
    # LIMIT/STOP na koncie live" (patrz historia w bot_engine.py) OBALONA
    # empirycznie 2026-08-06/07 - LIMIT i STOP-LIMIT ręcznie potwierdzone
    # działające na live. Mimo to boty NIE są odblokowane na live automatycznie
    # (patrz RiskSettings/SignalSettings/EODSettings.stop_loss_only_mode) -
    # to osobna, świadoma decyzja, nie techniczne ograniczenie.
    active_environment = db.Column(db.String(10), nullable=False, default="demo")

    user = db.relationship("User", back_populates="settings")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UserSettings user_id={self.user_id} cap={self.max_order_value}>"


class OrderLog(db.Model):
    """
    Log każdego zlecenia wysłanego (lub zablokowanego przez Hard Cap).

    status:
        "sent"      - zlecenie wysłane do T212 API
        "filled"    - potwierdzone wykonanie (jeśli/gdy synchronizujemy status)
        "rejected"  - odrzucone przez T212 (np. brak środków/akcji)
        "blocked"   - zablokowane lokalnie przez Hard Cap / cooldown,
                      NIGDY nie dotarło do T212 - ważne rozróżnienie do audytu
    """
    __tablename__ = "order_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    environment = db.Column(db.String(10), nullable=False)  # demo/live
    ticker = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(4), nullable=False)  # "buy" / "sell"
    quantity = db.Column(db.Numeric(12, 4), nullable=False)
    price_snapshot = db.Column(db.Numeric(12, 4), nullable=True)  # cena w momencie kliknięcia
    estimated_value = db.Column(db.Numeric(12, 2), nullable=True)  # quantity * price_snapshot

    status = db.Column(db.String(10), nullable=False)
    block_reason = db.Column(db.String(50), nullable=True)  # np. "HARD_CAP_EXCEEDED"

    t212_order_id = db.Column(db.String(64), nullable=True)  # id z odpowiedzi API, jeśli sent

    # Nullable - zlecenia z Warp Mode nie mają Pie. Wypełniane tylko gdy zlecenie
    # wyszło z widoku Smart Virtual Pie (routes/pie.py) albo (w przyszłości)
    # z Micro-Grid Bota. Brak ON DELETE CASCADE celowo - to zapis historyczny,
    # ma przetrwać skasowanie samego koszyka (patrz Pie.__doc__).
    pie_id = db.Column(db.Integer, db.ForeignKey("pies.id"), nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False, index=True)

    user = db.relationship("User", back_populates="orders")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderLog {self.side} {self.ticker} status={self.status}>"


class Pie(db.Model):
    """
    Prywatny "wirtualny ETF" - koszyk instrumentów z proporcjonalnymi wagami
    (patrz PieAsset), do wygodnego "dokupywania luzem" bez opłaty FX 0.15%.

    Należy do JEDNEGO użytkownika - kasowanie Pie kasuje jego PieAsset-y
    (cascade na poziomie ORM, tak jak User -> ApiKeySet/UserSettings/OrderLog
    wyżej w tym pliku; SQLite w tym projekcie nie ma włączonego wymuszania FK
    na poziomie bazy, więc trzymamy się tej samej konwencji co reszta modeli).

    Skasowanie Pie NIE kasuje powiązanych OrderLog (patrz OrderLog.pie_id) -
    historia zleceń ma przetrwać, nawet jeśli sam koszyk już nie istnieje.
    """
    __tablename__ = "pies"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)

    # Patrz identyczny komentarz przy BotAsset.environment - dodane 2026-08-07
    # po tym jak Adam zauważył że koszyki Virtual Pie stworzone na demo dalej
    # "wisiały" na liście po przełączeniu konta na live.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    user = db.relationship("User", back_populates="pies")
    assets = db.relationship(
        "PieAsset", back_populates="pie",
        cascade="all, delete-orphan", order_by="PieAsset.id",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Pie {self.name} user_id={self.user_id}>"


class PieAsset(db.Model):
    """
    Pojedyncze aktywo wewnątrz Pie wraz z jego docelową PROPORCJĄ
    (target_weight) - NIE sztywnym procentem. Wagi [1, 2, 1] oznaczają że
    środkowe aktywo dostaje 2x tyle budżetu co skrajne - normalizacja do %
    dzieje się WYŁĄCZNIE w warstwie prezentacji (routes/pie.py, static/js/pie.js),
    baza zawsze trzyma surowe, nieznormalizowane liczby.

    target_weight jako Numeric(12,4) (nie Float) - spójnie z OrderLog.quantity /
    UserSettings.default_quantity, żeby matematyka wag szła przez Decimal jak
    reszta kodu (risk_guard.py, t212_client.py), bez błędów zaokrągleń float.

    user_id: CELOWA DENORMALIZACJA (jest też Pie.user_id, do którego pie_id
    prowadzi) - szybkie odczyty bez JOIN-a. Bezpieczne, bo nie ma funkcji
    "przenieś aktywo do innego Pie" - user_id ustawiany raz przy tworzeniu,
    nigdy się nie rozjeżdża.

    UWAGA (zmiana decyzji z Etapu 2/część 2->3): Micro-Grid Bot NIE korzysta
    już z PieAsset. Pierwsza wersja miała tu is_bot_allowed/is_penny_stock/
    bot_entry_amount ("bot pożycza listę z Pie") - jawnie odrzucone przez
    użytkownika jako mylące ("co Pie ma do bota") - bot dostał WŁASNY,
    niezależny model (patrz BotAsset niżej). PieAsset dziś to WYŁĄCZNIE
    Smart Virtual Pie (ręczne dokupywanie), zero związku z botem.
    """
    __tablename__ = "pie_assets"
    __table_args__ = (
        db.UniqueConstraint("pie_id", "ticker", name="uq_pie_ticker"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    pie_id = db.Column(db.Integer, db.ForeignKey("pies.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)          # ticker T212, np. "AAPL_US_EQ"
    display_ticker = db.Column(db.String(20), nullable=False)  # krótka forma do UI, np. "AAPL"
    currency = db.Column(db.String(10), nullable=False)        # z Instrument.currency_code w momencie dodania

    target_weight = db.Column(db.Numeric(12, 4), nullable=False, default=1)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    pie = db.relationship("Pie", back_populates="assets")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PieAsset {self.ticker} pie_id={self.pie_id} w={self.target_weight}>"


class RiskSettings(db.Model):
    """
    Konfiguracja ryzyka bota Micro-Grid, JEDNA na użytkownika (unique=True,
    tak jak UserSettings) - w odróżnieniu od PieAsset.is_bot_allowed, które
    jest per-aktywo, to są globalne "twarde limity" dla całego bota danego
    usera.

    is_paper_trading domyślnie True - bezpieczny default zgodny z PRD.
    Etap 2 (ta część) NIC jeszcze nie sprawdza tej flagi (brak logiki
    handlowej) - kolumna istnieje już teraz, żeby kolejna część nie
    wymagała migracji.
    """
    __tablename__ = "risk_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)

    dca_scenario = db.Column(db.String(100), nullable=False, default="1,1,1,1,1")
    max_dca_levels = db.Column(db.Integer, nullable=False, default=5)

    # O ile % (wzgledem CENY WEJSCIA POZIOMU 0 - ActiveTrade.grid_anchor_price,
    # NIE ruchomej sredniej) ma spasc cena, zeby bot dokupil kolejny poziom
    # DCA - patrz services/bot_engine.py::_trigger_dca_buys. Brak tego pola w
    # pierwotnym PRD/schemacie - dodane na wyrazne zyczenie Adama (2026-07-20,
    # "polacz opcje 2 i 3": domyslnie 5%, ale edytowalne w UI zamiast na
    # sztywno w kodzie).
    dca_trigger_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.05)

    max_spread_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.05)

    # Legacy kolumna (2026-08-08) - USUNIĘTA z aktywnego użytku 2026-07-21 na
    # rzecz trailing stopu (patrz komentarz niżej), ale zostaje w schemacie
    # bazy jako NOT NULL bez defaultu (SQLite 3.34 na tym NAS-ie nie wspiera
    # DROP COLUMN, dodane dopiero w 3.35) - bez tego mapowania każdy NOWY
    # insert RiskSettings (czyli KAŻDY świeżo zarejestrowany user) wybuchał
    # IntegrityError, bo SQLAlchemy nie znało tej kolumny i nie podawało dla
    # niej żadnej wartości. Znalezione na żywo 2026-08-08 przy rejestracji
    # konta testowego na dev - /bot/ dawało 500.
    take_profit_usd = db.Column(db.Numeric(6, 4), nullable=False, default=0.05)

    # Trailing STOP (zamiast dawnego sztywnego take_profit_usd, 2026-07-21) -
    # patrz services/bot_engine.py::_manage_trailing_exit. Procent, nie stała
    # kwota - żeby krok/stop skalowały się z ceną instrumentu (Adam: sztywna
    # kwota EUR na drogiej spółce jak ASML to szum, na groszówce to przepaść).
    # JEDNO zlecenie na raz (przeprojektowane tego samego dnia - T212 nie
    # pozwala trzymać LIMIT SELL + STOP równocześnie na te same akcje, patrz
    # docs/IDEAS_v2.md pkt 4): bot NIE wystawia nic od razu po kupnie - czeka
    # aż cena minie 2 progi (take_profit_step_pct), dopiero wtedy uzbraja
    # pojedynczy STOP na average_price*(1-stop_loss_pct), i przesuwa TEN SAM
    # STOP w górę o kolejny próg za każdym razem gdy cena mija następny.
    take_profit_step_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.003)
    # stop_loss_pct: dystans STOP-a od average_price przy PIERWSZYM uzbrojeniu
    # (dokładnie na progu 2) - ochrona kapitału, nie stop od samego wejścia.
    # Przy kolejnych progach STOP przesuwa się wg take_profit_step_pct, nie
    # wg tego pola - patrz _manage_trailing_exit.
    stop_loss_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.02)

    max_daily_loss = db.Column(db.Numeric(12, 2), nullable=False, default=10.0)

    is_paper_trading = db.Column(db.Boolean, nullable=False, default=True)
    is_bot_active = db.Column(db.Boolean, nullable=False, default=False)

    # Switch "zarzadzaj wszystkim" (pomysl #1, docs/IDEAS_v2.md, zaimplementowany
    # 27.07.2026) - gdy wlaczony, kazdy ticker posiadany na koncie T212 bez
    # otwartej ActiveTrade zostaje automatycznie "adoptowany" (patrz
    # bot_engine.py::_auto_adopt_foreign_positions) pod trailing exit, BEZ DCA
    # (grid_anchor_price=0 - bot nie zna kontekstu recznego zakupu). Wylaczenie
    # switcha automatycznie zwalnia WYLACZNIE pozycje przejete tak automatycznie
    # (ActiveTrade.auto_adopted=True) - patrz routes/bot.py::_release_auto_adopted_positions.
    # Reczna adopcja przyciskiem "Przekaz botowi" NIE jest tym dotknieta.
    manage_all_positions = db.Column(db.Boolean, nullable=False, default=False)

    # Money management: skalowanie efektywnej kwoty wejscia/DCA wzgledem
    # BIEZACEGO equity kontra equity ODNIESIENIA (baseline), metoda
    # PIERWIASTKOWA (nie liniowa) - patrz microgrid_strategy.compute_equity_
    # scaled_amount. Wybor Adama po lekturze "Build Better Strategies" czesc 3
    # (2026-07-31): liniowe skalowanie %equity to WPROST opisany tam anti-pattern
    # (drawdown rosnie jak √T), Kelly/OptimalF odrzucone (za mala/zaszumiona
    # proba: 41 zamknietych transakcji z 9 dni historii, 56% bez znanego
    # close_price). DOMYSLNIE WYLACZONE - zero zmiany zachowania (effective_
    # amount == entry_amount) dopoki Adam swiadomie nie zaznaczy checkboxa w UI.
    equity_sizing_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # Equity konta W MOMENCIE wlaczenia powyzszego checkboxa - AUTO-CAPTURE przez
    # serwer (T212Client.get_cash()), NIE reczne wpisywanie liczby (patrz
    # routes/bot.py::update_settings) - punkt odniesienia dla ktorego mnoznik=1.
    # Nullable (brak sensu zanim ktokolwiek pierwszy raz wlaczy flage) -
    # traktowane jak "brak" w compute_equity_scaled_amount, gdy None/<=0.
    equity_sizing_baseline = db.Column(db.Numeric(12, 2), nullable=True, default=None)

    # Limit jednoczesnych otwartych pozycji Micro-Gridu - do 2026-08-03 była to
    # stała modułowa MAX_CONCURRENT_POSITIONS w bot_engine.py (patrz
    # docs/IDEAS_v2.md, dopasowanie do budżetu ~1000€ z 02/03.08.2026), teraz
    # przeniesiona do ustawień per-user na życzenie Adama - "to tylko ustawienia
    # fabryczne", ma się dać zmieniać w UI bez redeployu. Default=6 zachowuje
    # wartość ustaloną tamtego dnia (multi-window walk-forward, patrz tam).
    max_concurrent_positions = db.Column(db.Integer, nullable=False, default=6)

    # Koszt przewalutowania (FX) dla tickerow USD na koncie EUR - konto demo
    # (jedyne na ktorym dzialaja boty) ma JEDNA walute, potwierdzone przez
    # API (2026-08-03: {"currency": "EUR", ...}, jeden zbiorczy cash, zero
    # oddzielnego portfela USD) i oficjalna Polityke realizacji zamowien
    # T212 (pkt 17.3: koszt przewalutowania przy zakupie w walucie innej niz
    # depozytowa) - kazdy zakup/sprzedaz tickera USD kosztuje ~0.15% w kazda
    # strone (~0.3% round-trip). Bez tego bot moze "zamykac zysk" trailing
    # stopem na progu ktory po przewalutowaniu jest juz strata/zerem -
    # DOMYSLNIE WLACZONE (enabled=True), bo Micro-Grid mial to ZAWSZE
    # aktywne na sztywno (FX_ROUND_TRIP_PCT w bot_engine.py, od 22.07) - ta
    # migracja tylko czyni to widoczne/przelaczalne w UI, NIE zmienia
    # dotychczasowego zachowania. fx_fee_pct = koszt JEDNEJ nogi (nie
    # round-trip) - kod mnozy razy 2 tam gdzie potrzebne.
    fx_cost_adjustment_enabled = db.Column(db.Boolean, nullable=False, default=True)
    fx_fee_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.0015)

    # Tryb "tylko stop-loss" (2026-08-06, na wyrazne zyczenie Adama - "wylacz
    # wszystkie funkcjonalnosci bota poza jedna - ma ustawiac stoplossa na
    # aktywa ktore mu wskaze"). Gdy wlaczony, tick() w bot_engine.py CALKOWICIE
    # pomija _process_entries (nowe wejscia), _trigger_dca_buys (dokupywanie)
    # i _auto_adopt_foreign_positions (automatyczne przejmowanie obcych pozycji
    # - "wskaze" znaczy RECZNIE, patrz routes/bot.py::adopt_position) -
    # _manage_trailing_exit zostaje BEZ ZMIAN, wiec juz zarzadzane pozycje
    # (adoptowane recznie lub istniejace) nadal dostaja przesuwany trailing
    # stop. DOMYSLNIE WYLACZONE - zero zmiany zachowania dopoki Adam swiadomie
    # nie zaznaczy checkboxa w UI.
    stop_loss_only_mode = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RiskSettings user_id={self.user_id} active={self.is_bot_active}>"


class BotAsset(db.Model):
    """
    Aktywo obserwowane przez Micro-Grid Bota - CAŁKOWICIE NIEZALEŻNE od
    Smart Virtual Pie (PieAsset). Własna, dedykowana lista bota - dodajesz
    ticker bezpośrednio na stronie /bot/, RAZEM z kwotą wejścia (entry_amount
    jest tu WYMAGANE, nie nullable - w odróżnieniu od pierwszej wersji
    (PieAsset.bot_entry_amount, nullable), tutaj nie ma stanu pośredniego
    "dodane, ale bez kwoty" - samo bycie na tej liście = bot będzie handlował
    tym tickerem).

    Ten sam ticker może być jednocześnie w jakimś Pie (do ręcznego dokupowania)
    i na liście bota - to dwie NIEZALEŻNE rzeczy, żadnego związku między nimi.
    """
    __tablename__ = "bot_assets"
    __table_args__ = (
        db.UniqueConstraint("user_id", "ticker", "environment", name="uq_bot_asset_user_ticker_env"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)
    display_ticker = db.Column(db.String(20), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    entry_amount = db.Column(db.Numeric(12, 2), nullable=False)
    is_penny_stock = db.Column(db.Boolean, default=False, nullable=False)

    # Do jakiego konta T212 (demo/live) należy ten wpis - dodane 2026-08-07
    # po incydencie, gdzie Adam skasował klucz demo, przełączył na live, a
    # bot dalej "widział" stare pozycje demo bo NIC nigdy nie filtrowało po
    # środowisku (patrz utils.current_environment/UserSettings.active_environment,
    # 2026-08-06). Wszystkie query MUSZĄ filtrować po tym polu.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BotAsset {self.ticker} entry={self.entry_amount}>"


class ActiveTrade(db.Model):
    """
    Pozycja bota Micro-Grid (seria DCA) - w tej części tylko wejście
    (dca_level=0), ale schemat i Reconciliation Loop (patrz
    services/bot_engine.py) są już gotowe pod pętlę DCA.

    bot_asset_id (FK do BotAsset, NIE surowy ticker ani PieAsset) - bot
    operuje na WŁASNEJ liście, niezależnej od Pie (patrz BotAsset).
    ticker/currency i tak zdenormalizowane tutaj (ten sam powód co
    BotAsset.user_id - szybkie odczyty bez JOIN-a na gorącej ścieżce pętli bota).

    position_group_id: UUID v4 spinający całą serię DCA w jedną pozycję -
    RELOAD (przyszła funkcja) zachowuje ten sam position_group_id przy
    resecie dca_level do 0.
    """
    __tablename__ = "active_trades"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    bot_asset_id = db.Column(db.Integer, db.ForeignKey("bot_assets.id"), nullable=False, index=True)

    position_group_id = db.Column(db.String(36), nullable=False, index=True)
    ticker = db.Column(db.String(30), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    buy_order_id = db.Column(db.String(64), nullable=False)
    # Dawna noga LIMIT SELL (take-profit) z pierwszej wersji trailing exitu
    # (dwunożne ręczne OCO, patrz services/bot_engine.py::_manage_trailing_exit) -
    # od przeprojektowania 2026-07-21 na pojedynczy trailing STOP już nie jest
    # zakładana dla nowych pozycji. Zostaje jako pole migracyjne: jeśli jakaś
    # pozycja ma ją jeszcze ustawioną (założoną PRZED przeprojektowaniem),
    # _manage_trailing_exit() ją anuluje i zeruje przy najbliższej okazji.
    sell_order_id = db.Column(db.String(64), nullable=True)

    # "Gonienie" ceny LIMIT BUY (patrz services/bot_engine.py::_retry_pending_buys) -
    # jeśli cena rynkowa odjedzie powyżej wystawionego LIMIT BUY (nigdy się już
    # sam nie wypełni), bot anuluje i wystawia nowy po aktualnej cenie. Ten sam
    # wzorzec backoffu co sell_retry_count/next_sell_retry_at.
    buy_retry_count = db.Column(db.Integer, nullable=False, default=0)
    next_buy_retry_at = db.Column(db.DateTime, nullable=True)

    # Retry LIMIT SELL (patrz services/bot_engine.py::_retry_pending_sells) -
    # next_sell_retry_at=NULL oznacza "sprobuj przy najblizszym ticku",
    # sell_blocked=True oznacza blad T212 inny niz "selling-equity-not-owned"
    # (nigdy sam sie nie naprawi) - pozycja przestaje byc automatycznie
    # ponawiana, wymaga recznej interwencji.
    sell_retry_count = db.Column(db.Integer, nullable=False, default=0)
    next_sell_retry_at = db.Column(db.DateTime, nullable=True)
    sell_blocked = db.Column(db.Boolean, nullable=False, default=False)

    # Trailing STOP (patrz RiskSettings.take_profit_step_pct/stop_loss_pct i
    # services/bot_engine.py::_manage_trailing_exit). buy_confirmed sygnalizuje
    # "kupno rozliczone" (bot czeka na 2 progi zanim cokolwiek wystawi).
    # trail_milestone_steps - na którym progu jest obecnie uzbrojony STOP
    # (0 = jeszcze żaden). stop_order_id - JEDYNA aktywna noga wyjścia od
    # przeprojektowania 2026-07-21 (dawniej druga, ochronna noga obok
    # sell_order_id w ręcznym OCO - teraz sell_order_id już nie jest zakładane
    # dla nowych pozycji, patrz komentarz przy tym polu).
    buy_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    trail_milestone_steps = db.Column(db.Integer, nullable=False, default=0)
    stop_order_id = db.Column(db.String(64), nullable=True)
    # Cena, na jaką ustawiony jest AKTUALNY stop_order_id (zapisywana przy
    # każdym uzbrojeniu/przesunięciu w _manage_trailing_exit) - jedyny sposób
    # na przybliżoną cenę wyjścia bez odpytywania historii zleceń T212
    # (get_order_history, patrz t212_client.py - wymaga dodatkowego
    # zapytania w ciasnym rate limicie demo, a kształt pola z ceną wykonania
    # w odpowiedzi nie był jeszcze na żywo zweryfikowany). Realne wykonanie
    # STOP-a to Market Order po przebiciu tej ceny, więc może się nieznacznie
    # różnić (poślizg) - to przybliżenie, nie gwarancja co do grosza.
    stop_target_price = db.Column(db.Numeric(12, 4), nullable=True)
    # Weekendowe zawieszenie SL (Adam, 2026-08-10 - patrz
    # services/weekend_guard.py) - True MIĘDZY anulowaniem prawdziwego
    # zlecenia (pt 21:00) a jego przywróceniem (pon 11:00). Odróżnia
    # "świadomie zawieszone" (stop_order_id=None, TO pole True) od "jeszcze
    # nigdy nie uzbrojone" (stop_order_id=None, TO pole False) - bez tego
    # rozróżnienia zwykły tick natychmiast uzbroiłby nowy stop, cofając
    # zawieszenie w 60s (patrz _manage_trailing_exit - filtruje po tym polu).
    sl_suspended_for_weekend = db.Column(db.Boolean, nullable=False, default=False)
    # Zapisywane przy zamknięciu pozycji (_finalize_closed_trade) - kopia
    # stop_target_price z chwili zamknięcia, żeby zysk/strata zrealizowana
    # dało się policzyć bez grzebania w (skasowanym w międzyczasie 21.07)
    # dzienniku ani bez ponownego pytania T212. NULL dla pozycji zamkniętych
    # PRZED dodaniem tego pola (2026-07-21) - nie da się tego odtworzyć
    # wstecz bez dostępu do historii zleceń T212.
    close_price = db.Column(db.Numeric(12, 4), nullable=True)

    # Ile tickera user posiadał w portfolio T212 TUŻ PRZED złożeniem tego
    # zlecenia kupna (patrz services/bot_engine.py::_enter_position). Gdy
    # zlecenie zniknie z pending (wykonane/anulowane), _retry_pending_sells()
    # liczy filled = aktualne_owned - baseline_owned_quantity - odejmuje
    # WCZEŚNIEJSZE posiadanie tego tickera (np. z ręcznego tradingu), więc
    # nigdy nie sprzeda cudzej/starszej pozycji tego samego tickera.
    baseline_owned_quantity = db.Column(db.Numeric(12, 4), nullable=False, default=0)

    # Cena WEJŚCIA POZIOMU 0 (pierwszego zakupu), USTAWIANA RAZ w
    # _enter_position() i NIGDY później nie modyfikowana (w odróżnieniu od
    # buy_price, który _retry_pending_buys() nadpisuje przy "gonieniu" ceny) -
    # stały punkt odniesienia siatki DCA. Poziom N wyzwala się gdy cena
    # spadnie do grid_anchor_price * (1 - dca_trigger_pct * N), patrz
    # services/bot_engine.py::_trigger_dca_buys.
    grid_anchor_price = db.Column(db.Numeric(12, 4), nullable=False, default=0)

    # Zawieszona noga DCA w trakcie potwierdzania wykonania - CAŁKOWICIE
    # NIEZALEŻNE od buy_order_id/baseline_owned_quantity (tamte dotyczą
    # WYŁĄCZNIE pierwszego wejścia, poziom 0, i są już "rozwiązane" zanim
    # jakikolwiek poziom DCA może się wyzwolić - wymaga sell_order_id
    # ustawionego). Patrz _trigger_dca_buys/_confirm_dca_fills.
    dca_pending_buy_order_id = db.Column(db.String(64), nullable=True)
    dca_pending_quantity = db.Column(db.Numeric(12, 4), nullable=True)
    dca_pending_price = db.Column(db.Numeric(12, 4), nullable=True)
    dca_pending_baseline_quantity = db.Column(db.Numeric(12, 4), nullable=True)

    buy_price = db.Column(db.Numeric(12, 4), nullable=False)
    quantity = db.Column(db.Numeric(12, 4), nullable=False)
    allocated_value = db.Column(db.Numeric(12, 2), nullable=False)
    average_price = db.Column(db.Numeric(12, 4), nullable=False)
    dca_level = db.Column(db.Integer, nullable=False, default=0)

    status = db.Column(db.String(10), nullable=False, default="OPEN")

    # Prawdziwe zlecenie na T212 (False) vs symulacja bez wysylki do T212
    # (True) - patrz services/bot_engine.py::_enter_position. Reconciliation
    # Loop MUSI pomijac pozycje papierowe (nie ma czego uzgadniac z T212,
    # zadne zlecenie tam nigdy nie trafilo).
    is_paper = db.Column(db.Boolean, nullable=False, default=False)

    # True WYLACZNIE dla pozycji przejetych AUTOMATYCZNIE przez switch
    # RiskSettings.manage_all_positions (patrz bot_engine.py::
    # _auto_adopt_foreign_positions, 27.07.2026) - NIGDY dla recznej adopcji
    # przyciskiem "Przekaz botowi" (routes/bot.py::adopt_position). Odroznia
    # ktore pozycje ma automatycznie zwolnic wylaczenie switcha (patrz
    # routes/bot.py::_release_auto_adopted_positions) - reczna adopcja ZAWSZE
    # zostaje pod botem do recznego "Zwolnij", niezaleznie od stanu switcha.
    auto_adopted = db.Column(db.Boolean, nullable=False, default=False)

    # Denormalizowane (ten sam powód co ticker/currency wyżej - zero JOIN-a
    # na gorącej ścieżce) - patrz identyczny komentarz przy BotAsset.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ActiveTrade {self.ticker} dca={self.dca_level} status={self.status}>"


class BotAuditLog(db.Model):
    """
    Log DECYZJI/zdarzeń bota Micro-Grid - CELOWO osobny od OrderLog, który
    loguje KAŻDE zlecenie (łącznie z ręcznymi z Warp/Pie). BotAuditLog
    obejmuje też zdarzenia BEZ zlecenia (CANCEL, RELOAD, ERROR, heartbeat
    "tick", wpisy z Reconciliation Loop) - stąd nazwa "BotAuditLog", nie
    "AuditLog", żeby nie mylić z OrderLog.
    """
    __tablename__ = "bot_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    position_group_id = db.Column(db.String(36), nullable=True, index=True)

    action_type = db.Column(db.String(10), nullable=False)  # BUY/CANCEL/RELOAD/ERROR/INFO
    message = db.Column(db.Text, nullable=False)

    # Patrz identyczny komentarz przy BotAsset.environment (models.py) -
    # 2026-08-07, Adam: "w dziennikach botow zostala historia na live" -
    # bez tego Dziennik po przełączeniu demo/live pokazywał starą historię
    # z poprzedniego konta jako "aktualną".
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BotAuditLog {self.action_type} user_id={self.user_id}>"


# =============================================================================
# Strategia sygnałowa RSI/MA/ATR (services/signal_engine.py) - decyzja Adama
# 2026-07-24 (patrz docs/IDEAS_v2.md, "Otwarte pytania"): OSOBNA strategia,
# DZIAŁA RÓWNOLEGLE do Micro-Grid Bota (BotAsset/ActiveTrade wyżej), go NIE
# zastępuje. Ten sam ticker może być jednocześnie na obu listach jako dwie
# NIEZALEŻNE pozycje - stąd całkowicie własne tabele zamiast rozbudowy
# istniejących (ten sam wzorzec niezależności co BotAsset vs PieAsset).
# W odróżnieniu od Micro-Grid: JEDNO wejście na sygnał, bez siatki DCA -
# PRD nie przewiduje dokupowania dla tej strategii.
# =============================================================================

class SignalAsset(db.Model):
    """Ticker obserwowany przez strategię sygnałową - własna, niezależna lista (patrz komentarz wyżej)."""
    __tablename__ = "signal_assets"
    __table_args__ = (
        db.UniqueConstraint("user_id", "ticker", "environment", name="uq_signal_asset_user_ticker_env"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)
    display_ticker = db.Column(db.String(20), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    entry_amount = db.Column(db.Numeric(12, 2), nullable=False)

    # Patrz identyczny komentarz przy BotAsset.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SignalAsset {self.ticker} entry={self.entry_amount}>"


class SignalSettings(db.Model):
    """
    Konfiguracja ryzyka strategii sygnałowej, JEDNA na użytkownika (analogia
    do RiskSettings dla Micro-Grid, ale osobna tabela/kolumny - zero
    współdzielenia parametrów między strategiami).

    RSI(14)/MA(200) (okresy) są STAŁE modułowe w signal_engine.py, nie
    kolumny tutaj - PRD podaje je jako "warunek bazowy" strategii, nie
    parametr do stroju; próg RSI i mnożniki ATR (rzeczy, które Adam realnie
    będzie chciał kręcić "w boju" bez redeployu) SĄ tutaj edytowalne.
    """
    __tablename__ = "signal_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)

    is_active = db.Column(db.Boolean, nullable=False, default=False)
    # Domyślnie True - ten sam bezpieczny default co RiskSettings.is_paper_trading.
    is_paper_trading = db.Column(db.Boolean, nullable=False, default=True)

    rsi_threshold = db.Column(db.Numeric(6, 2), nullable=False, default=35)
    stop_loss_atr_mult = db.Column(db.Numeric(6, 2), nullable=False, default=1.8)
    take_profit_atr_mult = db.Column(db.Numeric(6, 2), nullable=False, default=3.0)

    # Limit jednoczesnych otwartych pozycji - do 2026-08-03 stała modułowa
    # signal_engine.MAX_CONCURRENT_POSITIONS (dodana wieczorem 02/03.08.2026,
    # patrz docs/IDEAS_v2.md), teraz ustawienie per-user, edytowalne w UI
    # (ten sam powód co RiskSettings.max_concurrent_positions).
    max_concurrent_positions = db.Column(db.Integer, nullable=False, default=2)

    # Money management √equity - ten sam mechanizm co RiskSettings (Micro-Grid,
    # 2026-07-31/2026-08-03 wieczorem), teraz też dla Sygnału (Adam: "a jak
    # myślisz ma to sens tam?" -> "dodaj do obu"). Sygnał nie ma DCA (jedna
    # noga, brak mnożenia ryzyka przez poziomy uśredniania) - mniej pilne niż
    # w Micro-Gridzie, ale ten sam problem strukturalny istnieje: entry_amount
    # jest STAŁĄ absolutną kwotą, więc przy kurczącym się equity ryzyko na
    # transakcję względem kapitału ROŚNIE (dokładnie to, przed czym broni
    # skalowanie √equity). Patrz microgrid_strategy.compute_equity_scaled_amount
    # (funkcja ogólna, nie specyficzna dla DCA - używana też tutaj wprost).
    equity_sizing_enabled = db.Column(db.Boolean, nullable=False, default=False)
    equity_sizing_baseline = db.Column(db.Numeric(12, 2), nullable=True, default=None)

    # Koszt przewalutowania (FX) dla tickerow USD - ten sam mechanizm i
    # uzasadnienie co RiskSettings.fx_cost_adjustment_enabled (Micro-Grid
    # mial to na sztywno od 22.07.2026, Sygnał nigdy nie miał - znalezione
    # 2026-08-03, Adam: "dodaj w ustawieniach switch... bo inaczej możemy
    # tracić mimo że będziemy zyskiwać"). DOMYŚLNIE WŁĄCZONE - to naprawa
    # realnej luki, nie eksperymentalna funkcja, Adam chce to aktywne od razu.
    fx_cost_adjustment_enabled = db.Column(db.Boolean, nullable=False, default=True)
    fx_fee_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.0015)

    # Tryb "tylko stop-loss" - ten sam mechanizm i uzasadnienie co
    # RiskSettings.stop_loss_only_mode (Micro-Grid, 2026-08-06), rozszerzony
    # na Sygnał 2026-08-06 (ciąg dalszy tej samej nocy - Adam podłącza
    # PRAWDZIWE konto T212 na prod, chce mieć pewność że ŻADEN z 3 silników
    # nie otworzy nic nowego, dopóki świadomie tego nie odblokuje). Gdy
    # włączone, tick() pomija _process_entries całkowicie - _manage_exits
    # (trailing stop-loss/take-profit) zostaje bez zmian. DOMYŚLNIE WYŁĄCZONE.
    stop_loss_only_mode = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SignalSettings user_id={self.user_id} active={self.is_active}>"


class SignalTrade(db.Model):
    """
    Pozycja otwarta przez strategię sygnałową - JEDNO wejście na sygnał, bez
    poziomów DCA (w odróżnieniu od ActiveTrade/Micro-Grid).

    Wyjście: POJEDYNCZY resting STOP na T212 (stop_order_id, ochrona nawet
    gdy appka/bot offline - ten sam powód co trailing STOP Micro-Grid) +
    take-profit pilnowany WYŁĄCZNIE w softwarze (take_profit_price, sprzedaż
    Market gdy żywa cena go dotknie) - T212 nie pozwala trzymać dwóch
    jednoczesnych resting orderów na te same udziały (potwierdzone na żywo
    21.07, patrz docs/IDEAS_v2.md pkt 4), więc druga noga MUSI być
    programowa, nie prawdziwe zlecenie.
    """
    __tablename__ = "signal_trades"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    signal_asset_id = db.Column(db.Integer, db.ForeignKey("signal_assets.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    buy_order_id = db.Column(db.String(64), nullable=False)
    # Ten sam wzorzec co ActiveTrade.baseline_owned_quantity - ile tickera
    # user posiadał TUŻ PRZED złożeniem tego zlecenia, żeby potwierdzenie
    # wypełnienia (current_owned - baseline) nigdy nie policzyło cudzej/
    # wcześniejszej pozycji tego samego tickera jako "swojej".
    baseline_owned_quantity = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    buy_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    # Gonienie ceny LIMIT BUY (dodane 2026-07-30, patrz bot_engine.py::
    # _retry_pending_buys/ActiveTrade.buy_retry_count - ten sam wzorzec, ten
    # sam powod: znaleziony na zywo IFXd_EQ, ktore utknelo na 9+ godzin bo
    # cena uciekla od limitu, a Sygnal do tej pory nie mial ZADNEGO
    # mechanizmu ponawiania).
    buy_retry_count = db.Column(db.Integer, nullable=False, default=0)
    next_buy_retry_at = db.Column(db.DateTime, nullable=True)

    buy_price = db.Column(db.Numeric(12, 4), nullable=False)
    quantity = db.Column(db.Numeric(12, 4), nullable=False)
    allocated_value = db.Column(db.Numeric(12, 2), nullable=False)

    atr_at_entry = db.Column(db.Numeric(12, 4), nullable=False)
    stop_loss_price = db.Column(db.Numeric(12, 4), nullable=False)
    take_profit_price = db.Column(db.Numeric(12, 4), nullable=False)
    stop_order_id = db.Column(db.String(64), nullable=True)
    # Weekendowe zawieszenie SL - patrz identyczny komentarz na
    # ActiveTrade.sl_suspended_for_weekend / services/weekend_guard.py.
    sl_suspended_for_weekend = db.Column(db.Boolean, nullable=False, default=False)

    status = db.Column(db.String(10), nullable=False, default="OPEN")
    is_paper = db.Column(db.Boolean, nullable=False, default=False)

    close_price = db.Column(db.Numeric(12, 4), nullable=True)
    closed_via = db.Column(db.String(20), nullable=True)  # "stop-loss" / "take-profit" / "manual"

    # Denormalizowane, patrz identyczny komentarz przy ActiveTrade.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SignalTrade {self.ticker} status={self.status}>"


class SignalAuditLog(db.Model):
    """Log strategii sygnałowej - analogia do BotAuditLog, osobna tabela (patrz komentarz nad SignalAsset)."""
    __tablename__ = "signal_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    action_type = db.Column(db.String(10), nullable=False)  # BUY/SELL/ERROR/INFO
    message = db.Column(db.Text, nullable=False)

    # Patrz identyczny komentarz przy BotAuditLog.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SignalAuditLog {self.action_type} user_id={self.user_id}>"


# =============================================================================
# Moduł EOD (End of Day) - services/eod_engine.py - TRZECI, znowu CAŁKOWICIE
# OSOBNY silnik (patrz uzasadnienie niezależności przy SignalAsset wyżej,
# ten sam powód: "Moduł EOD to w praktyce osobny silnik, nie dodatek do
# tick()", docs/IDEAS_v2.md, ustalenia 2026-07-21). Działa WYŁĄCZNIE pod
# koniec sesji EUR (16:00-17:30 Amsterdam) na świecach 1-MINUTOWYCH
# (price_feed.get_eod_intraday_1m, Yahoo nieoficjalne - patrz docs/IDEAS_v2.md
# pkt 3, decyzja 2026-07-24) - reaguje na OSTRY spadek w 1-5 minut, nie na
# powolne pełzanie. Pozycje z tego modułu zostają otwarte tak samo jak w
# Sygnale (BEZ wymuszonego zamknięcia na koniec dnia - Adam 2026-07-24
# świadomie odrzucił sugestię PRD "nie przenosić na kolejny dzień").
# =============================================================================

class EODAsset(db.Model):
    """
    Lista 'High Conviction' spółek dla modułu EOD (PRD: "Możliwość wyboru
    listy High Conviction spółek, na których działa moduł EOD") - własna,
    niezależna od BotAsset/SignalAsset. entry_amount to BAZOWA kwota - realna
    wielkość wejścia mnoży ją przez tier zależny od ostrości spadku (patrz
    eod_engine._size_multiplier_for_drop).
    """
    __tablename__ = "eod_assets"
    __table_args__ = (
        db.UniqueConstraint("user_id", "ticker", "environment", name="uq_eod_asset_user_ticker_env"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)
    display_ticker = db.Column(db.String(20), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    entry_amount = db.Column(db.Numeric(12, 2), nullable=False)

    # Patrz identyczny komentarz przy BotAsset.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EODAsset {self.ticker} entry={self.entry_amount}>"


class EODSettings(db.Model):
    """Konfiguracja ryzyka modułu EOD, JEDNA na użytkownika - własne kolumny, zero współdzielenia z Micro-Grid/Sygnał."""
    __tablename__ = "eod_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)

    is_active = db.Column(db.Boolean, nullable=False, default=False)
    is_paper_trading = db.Column(db.Boolean, nullable=False, default=True)

    # Ciasne progi (PRD: "natychmiastowy ciasny Take Profit 0.4-0.9%",
    # "bardzo ciasny trailing stop 0.3-0.5%") - v1 ma je STAŁE od wejścia
    # (nie trailing, świadome uproszczenie jak w Sygnale - "sprawdzimy w boju").
    stop_loss_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.004)
    take_profit_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.006)

    # PRD sugerował sztywne wymuszone zamknięcie pozycji EOD przed końcem
    # sesji ("nie przenoszą się na kolejny dzień") - Adam (2026-07-24) odrzucił
    # to jako STAŁE zachowanie, ale chce mieć możliwość włączenia go z powrotem
    # jako opcjonalny przełącznik zamiast twardo zakodowanego zachowania.
    # Domyślnie WYŁĄCZONE (pozycje zostają otwarte, zarządzane tylko stop-lossem/
    # take-profitem, jak w Sygnale) - patrz eod_engine.py::FORCE_CLOSE_TIME.
    force_close_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # Limit jednoczesnych otwartych pozycji - EOD do 2026-08-03 nie miał
    # ŻADNEGO capa (luka tej samej klasy co Sygnał przed wieczorną poprawką
    # 02/03.08.2026, patrz docs/IDEAS_v2.md) - dodane per-user, edytowalne w UI,
    # zamiast stałej modułowej (Adam: "to tylko ustawienia fabryczne").
    max_concurrent_positions = db.Column(db.Integer, nullable=False, default=2)

    # Money management √equity - ten sam mechanizm co RiskSettings/SignalSettings
    # (patrz komentarz przy SignalSettings.equity_sizing_enabled - ten sam
    # uzasadnienie, dodane razem 2026-08-03 wieczorem).
    equity_sizing_enabled = db.Column(db.Boolean, nullable=False, default=False)
    equity_sizing_baseline = db.Column(db.Numeric(12, 2), nullable=True, default=None)

    # Koszt przewalutowania (FX) dla tickerow USD - ten sam mechanizm i
    # uzasadnienie co RiskSettings/SignalSettings.fx_cost_adjustment_enabled
    # (EOD ma najciasniejsze progi % ze wszystkich trzech silnikow, wiec
    # najbardziej narazony - patrz docs/IDEAS_v2.md). DOMYŚLNIE WŁĄCZONE.
    fx_cost_adjustment_enabled = db.Column(db.Boolean, nullable=False, default=True)
    fx_fee_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.0015)

    # Tryb "tylko stop-loss" - patrz identyczny komentarz przy
    # RiskSettings.stop_loss_only_mode/SignalSettings.stop_loss_only_mode.
    # DOMYŚLNIE WYŁĄCZONE.
    stop_loss_only_mode = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EODSettings user_id={self.user_id} active={self.is_active}>"


class EODTrade(db.Model):
    """
    Pozycja modułu EOD - JEDNO wejście na trigger, bez DCA. drop_pct_at_entry/
    size_multiplier zapisane do audytu (żeby dało się ocenić czy tier
    sizingu działał sensownie). Mechanika wyjścia TA SAMA co SignalTrade
    (prawdziwy resting STOP dla stop-loss, take-profit pilnowany w
    softwarze - T212 nie pozwala na dwa resting ordery na te same udziały).
    Wymuszone zamknięcie na koniec dnia jest OPCJONALNE (EODSettings.
    force_close_enabled, domyślnie wyłączone) - bez niego pozycje zostają
    otwarte tak długo, aż trafi je stop-loss albo take-profit, jak w Sygnale.
    """
    __tablename__ = "eod_trades"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    eod_asset_id = db.Column(db.Integer, db.ForeignKey("eod_assets.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    buy_order_id = db.Column(db.String(64), nullable=False)
    baseline_owned_quantity = db.Column(db.Numeric(12, 4), nullable=False, default=0)
    buy_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    # Gonienie ceny LIMIT BUY (dodane 2026-07-30) - patrz identyczny komentarz
    # przy SignalTrade.buy_retry_count wyzej, ten sam powod/wzorzec.
    buy_retry_count = db.Column(db.Integer, nullable=False, default=0)
    next_buy_retry_at = db.Column(db.DateTime, nullable=True)

    buy_price = db.Column(db.Numeric(12, 4), nullable=False)
    quantity = db.Column(db.Numeric(12, 4), nullable=False)
    allocated_value = db.Column(db.Numeric(12, 2), nullable=False)

    drop_pct_at_entry = db.Column(db.Numeric(6, 4), nullable=False)
    size_multiplier = db.Column(db.Numeric(4, 2), nullable=False)

    stop_loss_price = db.Column(db.Numeric(12, 4), nullable=False)
    take_profit_price = db.Column(db.Numeric(12, 4), nullable=False)
    stop_order_id = db.Column(db.String(64), nullable=True)
    # Weekendowe zawieszenie SL - patrz identyczny komentarz na
    # ActiveTrade.sl_suspended_for_weekend / services/weekend_guard.py.
    sl_suspended_for_weekend = db.Column(db.Boolean, nullable=False, default=False)

    status = db.Column(db.String(10), nullable=False, default="OPEN")
    is_paper = db.Column(db.Boolean, nullable=False, default=False)

    close_price = db.Column(db.Numeric(12, 4), nullable=True)
    closed_via = db.Column(db.String(20), nullable=True)  # "stop-loss" / "take-profit" / "eod-forced" / "manual"

    # Denormalizowane, patrz identyczny komentarz przy ActiveTrade.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EODTrade {self.ticker} status={self.status}>"


class EODAuditLog(db.Model):
    """Log modułu EOD - analogia do SignalAuditLog/BotAuditLog."""
    __tablename__ = "eod_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    action_type = db.Column(db.String(10), nullable=False)  # BUY/SELL/ERROR/INFO
    message = db.Column(db.Text, nullable=False)

    # Patrz identyczny komentarz przy BotAuditLog.environment.
    environment = db.Column(db.String(10), nullable=False, default="demo", index=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EODAuditLog {self.action_type} user_id={self.user_id}>"
