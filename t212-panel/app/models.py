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
    max_spread_pct = db.Column(db.Numeric(6, 4), nullable=False, default=0.05)
    take_profit_usd = db.Column(db.Numeric(12, 4), nullable=False, default=0.05)
    max_daily_loss = db.Column(db.Numeric(12, 2), nullable=False, default=10.0)

    is_paper_trading = db.Column(db.Boolean, nullable=False, default=True)
    is_bot_active = db.Column(db.Boolean, nullable=False, default=False)

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
        db.UniqueConstraint("user_id", "ticker", name="uq_bot_asset_user_ticker"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    ticker = db.Column(db.String(30), nullable=False)
    display_ticker = db.Column(db.String(20), nullable=False)
    currency = db.Column(db.String(10), nullable=False)

    entry_amount = db.Column(db.Numeric(12, 2), nullable=False)
    is_penny_stock = db.Column(db.Boolean, default=False, nullable=False)

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
    sell_order_id = db.Column(db.String(64), nullable=True)

    buy_price = db.Column(db.Numeric(12, 4), nullable=False)
    quantity = db.Column(db.Numeric(12, 4), nullable=False)
    allocated_value = db.Column(db.Numeric(12, 2), nullable=False)
    average_price = db.Column(db.Numeric(12, 4), nullable=False)
    dca_level = db.Column(db.Integer, nullable=False, default=0)

    status = db.Column(db.String(10), nullable=False, default="OPEN")

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

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BotAuditLog {self.action_type} user_id={self.user_id}>"
