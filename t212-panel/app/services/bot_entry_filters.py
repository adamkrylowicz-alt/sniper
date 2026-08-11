"""
app/services/bot_entry_filters.py
===================================
Wybor MOMENTU i AKTYWA przy wejsciu bota (dca_level=0) + bezpiecznik dziennej
straty. Wydzielone do osobnego modulu 2026-07-23 - bot_engine.py ma juz ~1000
linii, a to jest samodzielny kawalek decyzyjny ktory da sie testowac osobno.

DLACZEGO TO POWSTALO (analiza z 23.07):
Sciezka do zysku w tym bocie jest waska - po wejsciu cena musi wzrosnac o DWA
progi zeby trailing STOP w ogole sie uzbroil (_manage_trailing_exit), a kazdy
inny scenariusz to DCA w dol. Czyli MOMENT WEJSCIA jest zmienna dominujaca,
a nie trailing/ATR/DCA. A do 23.07 moment wejscia byl w praktyce losowy:

1. _process_entries iterowalo BotAsset.query...all() BEZ order_by - o tym
   ktore 10 z 38 kandydatow zostanie otwarte decydowala kolejnosc wierszy
   w bazie i to kto akurat nie siedzial w backoffie.
2. Filtr trendu byl JEDNOSTRONNY (tylko spadek >3%) - instrument ktory
   wystrzelil +8% w 3 dni przechodzil bez zajaknienia, a to czesto najgorszy
   moment na wejscie.
3. Nikt nie sprawdzal GDZIE w dzisiejszym zakresie jest cena - bot rownie
   chetnie kupowal przy szczycie dnia co przy dnie.

ZERO NOWYCH ZAPYTAN DO API: wszystko liczone ze swiec ktore bot i tak juz
pobiera (price_feed.get_mini_chart_ohlc ma wlasny cache 30 min, uzywany juz
przez filtr trendu i ATR w bot_engine).

--- Filtr regime'u (Hurst Exponent), dodany 2026-07-31 --------------------
Po lekturze serii "Build Better Strategies" (financial-hacker.com, cz.2):
_trend_ok wyzej odpowiada na pytanie "czy TERAZ jest dobry moment wejscia"
(krotkie okno, lapanie spadajacego noza/wystrzalu). Hurst Exponent odpowiada
na INNE pytanie - "czy ten instrument W OGOLE zachowuje sie jak kandydat do
mean-reversion" (Hurst<0.5) czy raczej trenduje (Hurst>0.5, gridowanie w dol
trendu regularnie przegrywa). To DODATKOWY filtr, nie zastepstwo - dwie rozne
skale czasowe, obie potrzebne. Wymaga duzo dluzszego okna (~120 dni) niz
TREND_LOOKBACK_DAYS (10) - Hurst na garstce punktow to szum, nie sygnal.
DOMYSLNIE WYLACZONY (HURST_FILTER_ENABLED=False) - nowy filtr wplywajacy na
realne wejscia bota wymaga backtestu PRZED wlaczeniem na produkcji (patrz
run_microgrid_backtest.py --hurst-filter), zgodnie z zasada "nigdy nie zgaduj
nowego parametru ryzyka".
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from decimal import Decimal

from flask import current_app

from ..extensions import db
from ..models import ActiveTrade, RiskSettings
from ..utils import current_environment, fx_adjusted_cost_basis

# --- Filtr 1: pozycja ceny w dzisiejszym zakresie ------------------------
# (cena - low_dnia) / (high_dnia - low_dnia): 0.0 = dokladnie przy dnie dnia,
# 1.0 = przy szczycie. Dla strategii celujacej w +0.3% netto to jest roznica
# miedzy "mam bufor na ruch w gore" a "od razu wchodze w DCA".
# 0.6 = odrzucamy wejscia w gornych 40% dzisiejszej swiecy.
MAX_ENTRY_RANGE_POSITION = Decimal("0.6")

# Gdy dzisiejszy zakres jest absurdalnie waski (h ~= l, np. tuz po otwarciu
# albo instrument prawie nie handlowany), range position jest matematycznie
# bez sensu (dzielenie przez ~0) - wtedy filtr sie nie stosuje, patrz
# _range_position(). Prog jako UŁAMEK ceny, nie wartosc bezwzgledna, zeby
# dzialal tak samo dla akcji za 5 i za 900.
MIN_DAY_RANGE_FRACTION = Decimal("0.001")  # 0.1% ceny

# --- Filtr 2: trend dwustronny -------------------------------------------
# Stary ENTRY_TREND_MAX_DROP_PCT (3% w bot_engine) lapal tylko spadki.
# Tu obie strony: spadek = "lapanie spadajacego noza", wystrzal = mean
# reversion dziala przeciw nam.
MAX_TREND_DROP_PCT = Decimal("0.03")
MAX_TREND_SPIKE_PCT = Decimal("0.06")

# Stary filtr porownywal TYLKO candles[0].c z candles[-1].c - spadek -10% z
# odbiciem do -2% wygladal identycznie jak spokojny dryf -2%. Dodatkowo wiec
# patrzymy na MAKSYMALNE obsuniecie w oknie (od szczytu do dolka) - duza
# amplituda przy plaskim wyniku netto to nie jest "spokojny instrument".
MAX_TREND_DRAWDOWN_PCT = Decimal("0.08")

# UWAGA na dni kalendarzowe vs sesyjne: days=6 to realnie ~4 sesje
# (weekendy). Bierzemy 10 dni kalendarzowych zeby miec ~7 sesji probki.
TREND_LOOKBACK_DAYS = 10

# Gorny limit ile z otrzymanej listy swiec liczy sie jako "okno niedawne" dla
# _range_position/_trend_metrics. USTAWIONE NA WARTOSC >= max mozliwej dlugosci
# dzisiejszego fetchu (days=TREND_LOOKBACK_DAYS=10 kalendarzowych daje NAJWYZEJ
# 10 wierszy, patrz price_feed.py `candles[-days:]`) - dzieki temu
# candles[-RECENT_WINDOW_ROWS:] jest NO-OPEM przy dzisiejszym rozmiarze fetchu,
# a dopiero gdy HURST_FILTER_ENABLED podniesie fetch do HURST_LOOKBACK_DAYS
# (znacznie dluzszy), realnie przycina do ostatnich dni - patrz sekcja Hurst
# nizej po pelne wyjasnienie.
RECENT_WINDOW_ROWS = 10

# --- Filtr 4: regime (Hurst Exponent) - patrz docstring modulu ------------
HURST_FILTER_ENABLED = False  # patrz docstring modulu - domyslnie WYLACZONY
HURST_LOOKBACK_DAYS = 120     # potrzeba duzo dluzszego okna niz TREND_LOOKBACK_DAYS
HURST_MIN_SAMPLES = 40        # ponizej tego wynik to szum - fail-open (jak inne filtry)
HURST_MAX_FOR_ENTRY = Decimal("0.5")  # >=0.5 = trenduje, zle dla gridu - odrzucamy

# --- Filtr 3: spread ------------------------------------------------------
# Przy celu +0.3% netto (JUZ po round-tripie FX) spread 0.2% zjada dwie
# trzecie zysku zanim cokolwiek sie wydarzy. To nie jest optymalizacja, to
# filtr "czy ta transakcja ma w ogole sens matematyczny".
# Prog bierzemy z RiskSettings.max_spread_pct (pole juz istnieje w modelu i
# w formularzu - do 23.07 bylo martwe, nikt go nie czytal).
# Gdy zrodlo bid/ask nie jest dostepne -> filtr sie NIE stosuje (fail-open),
# ale jest to LICZONE i raportowane, patrz EntryStats.

# --- Scoring --------------------------------------------------------------
# Kandydaci ktorzy przeszli twarde filtry sa sortowani malejaco po score i
# bot wchodzi w NAJLEPSZEGO, zamiast w pierwszego-lepszego z bazy.
# Wagi jako stale modulu - do strojenia bez grzebania w logice.
W_RANGE_POSITION = Decimal("1.0")   # im nizej w dzisiejszym zakresie, tym lepiej
W_TREND_CALM = Decimal("0.6")       # preferuj spokojny/lekko rosnacy trend
W_FX_PENALTY = Decimal("0.8")       # USD musi zarobic wiecej zeby wyjsc na to samo
W_SPREAD = Decimal("0.5")           # weższy spread = wiecej z ruchu zostaje dla nas

# Kara dla USD w scoringu. FX_ROUND_TRIP_PCT z bot_engine to 0.3% - instrument
# USD musi zarobic 0.3 pkt proc. WIECEJ niz EUR-owy zeby dac ten sam wynik.
# Do 23.07 obie grupy konkurowaly o sloty na rownych prawach, mimo ze koszt
# byl juz poprawnie doliczany przy WYJSCIU (_manage_trailing_exit).
FX_ROUND_TRIP_PCT = Decimal("0.003")


class EntryStats:
    """
    Licznik "dlaczego kandydaci odpadli" dla JEDNEGO przebiegu _process_entries.

    Powod istnienia (punkt 4 analizy z 23.07): filtry dzialaja fail-open -
    gdy brak swiec (429 od Finnhuba / brak pokrycia symbolu), przepuszczaja
    wejscie zamiast je blokowac. To rozsadny default, ALE oznacza ze filtr
    cicho sie wylacza dokladnie wtedy gdy API jest pod obciazeniem - i nie
    bylo jak sie dowiedziec czy w ogole masz ochrone o ktorej myslisz ze masz.
    Teraz kazdy przebieg raportuje ile ocen bylo REALNYCH, a ile pominietych
    z braku danych.
    """

    def __init__(self) -> None:
        self.considered = 0
        self.rejected_range = 0
        self.rejected_trend = 0
        self.rejected_spread = 0
        self.rejected_regime = 0
        self.skipped_no_candles = 0
        self.skipped_no_spread_data = 0
        self.skipped_no_regime_data = 0
        self.passed = 0

    def summary(self) -> str:
        parts = [
            f"rozwazonych {self.considered}",
            f"przeszlo {self.passed}",
        ]
        if self.rejected_range:
            parts.append(f"odrzuconych (wysoko w zakresie dnia) {self.rejected_range}")
        if self.rejected_trend:
            parts.append(f"odrzuconych (trend) {self.rejected_trend}")
        if self.rejected_spread:
            parts.append(f"odrzuconych (spread) {self.rejected_spread}")
        if self.rejected_regime:
            parts.append(f"odrzuconych (regime/Hurst) {self.rejected_regime}")
        if self.skipped_no_candles:
            parts.append(f"BEZ OCENY trendu/zakresu (brak swiec) {self.skipped_no_candles}")
        if self.skipped_no_spread_data:
            parts.append(f"bez oceny spreadu (brak bid/ask) {self.skipped_no_spread_data}")
        if self.skipped_no_regime_data:
            parts.append(f"bez oceny regime'u (za krotka historia) {self.skipped_no_regime_data}")
        return ", ".join(parts)


def _d(value) -> Decimal:
    return Decimal(str(value))


def _range_position(candles: list[dict] | None, current_price: Decimal) -> Decimal | None:
    """
    Gdzie jest cena w DZISIEJSZYM zakresie: 0.0 = przy dnie, 1.0 = przy szczycie.
    None gdy nie da sie policzyc (brak swiec albo zakres praktycznie zerowy) -
    wywolujacy traktuje to jako "nie oceniam", nie jako "odrzuc".

    Ostatnia swieca z get_mini_chart_ohlc to biezaca sesja, wiec h/l mamy
    za darmo - zero dodatkowych zapytan.
    """
    if not candles:
        return None

    today = candles[-1]
    high = _d(today.get("h", 0))
    low = _d(today.get("l", 0))
    span = high - low

    if high <= 0 or span <= 0:
        return None
    if span < high * MIN_DAY_RANGE_FRACTION:
        return None  # zakres tak waski ze wskaznik nic nie znaczy

    position = (current_price - low) / span
    # Cena moze wyjsc poza h/l swiecy (swieca z cache sprzed max 30 min,
    # cena live jest swiezsza) - przycinamy do [0,1] zamiast zwracac dziwne
    # wartosci ktore zepsuja scoring.
    return min(max(position, Decimal("0")), Decimal("1"))


def _trend_metrics(candles: list[dict] | None) -> dict | None:
    """
    Zwraca {"net_pct", "drawdown_pct"} dla okna swiec, albo None gdy brak
    danych. net_pct > 0 = wzrost. drawdown_pct = najwieksze obsuniecie od
    szczytu kroczacego do pozniejszego dolka (zawsze >= 0).
    """
    if not candles or len(candles) < 3:
        return None

    closes = [_d(c.get("c", 0)) for c in candles if _d(c.get("c", 0)) > 0]
    if len(closes) < 3:
        return None

    first, last = closes[0], closes[-1]
    if first <= 0:
        return None

    net_pct = (last - first) / first

    peak = closes[0]
    max_drawdown = Decimal("0")
    for close in closes:
        if close > peak:
            peak = close
        if peak > 0:
            drawdown = (peak - close) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown

    return {"net_pct": net_pct, "drawdown_pct": max_drawdown}


def _trend_ok(metrics: dict | None) -> bool:
    """
    Filtr DWUSTRONNY (do 23.07 lapal tylko spadki). None (brak danych) ->
    True, czyli fail-open - patrz EntryStats po uzasadnienie i licznik.
    """
    if metrics is None:
        return True

    net = metrics["net_pct"]
    if net < -MAX_TREND_DROP_PCT:
        return False  # spadajacy noz
    if net > MAX_TREND_SPIKE_PCT:
        return False  # wystrzal - mean reversion dziala przeciw nam
    if metrics["drawdown_pct"] > MAX_TREND_DRAWDOWN_PCT:
        return False  # plaski wynik netto, ale po drodze duza amplituda
    return True


def _linear_regression_slope(xs: list[float], ys: list[float]) -> float:
    """Nachylenie prostej najmniejszych kwadratow (metoda momentow) - bez
    numpy (nie ma go w requirements.txt, Python 3.8 na NAS), n zawsze male
    (<= HURST_LOOKBACK_DAYS/2), wiec czysta petla Pythona wystarcza."""
    n = len(xs)
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    denominator = sum((x - xbar) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    numerator = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    return numerator / denominator


def compute_hurst_exponent(closes: list[Decimal]) -> Decimal | None:
    """
    Szacuje wykladnik Hursta metoda skalowania wariancji roznic cenowych po
    logu (klasyczne podejscie, patrz bibliografia Ernie Chan w Czesci 2
    "Build Better Strategies"): dla lagow n=2..HURST_MAX_LAG liczymy
    odchylenie standardowe roznic cen oddalonych o n dni. Z DEFINICJI
    ulamkowego ruchu Browna Var(roznica o lag n) ~ n^(2H), czyli
    odchylenie_std ~ n^H - nachylenie regresji log(odchylenie) ~ log(n) TO
    JUZ JEST szacowany Hurst wprost, BEZ dodatkowego mnozenia (zlapane w
    teście 2026-07-31: pierwsza wersja mylnie mnożyła nachylenie razy 2,
    myląc to ze skalowaniem samej wariancji zamiast odchylenia std).
    H<0.5 = mean-reversion (dobre dla gridu), H>0.5 = trend (zle - grid
    DCA-uje w dol calego trendu). H~0.5 = random walk (przyrosty iid,
    NIEZALEZNIE od tego czy cena ma deterministyczny dryf w gore/dol -
    Hurst mierzy autokorelacje PRZYROSTOW, nie sam kierunek ceny).

    Zwraca None gdy za malo probek (< HURST_MIN_SAMPLES) - TRAKTOWANE JAKO
    BRAK DANYCH przez wywolujacego (fail-open, jak inne filtry w tym module
    przy awarii Finnhub/Yahoo), NIE jako "odrzuc kandydata".
    """
    if len(closes) < HURST_MIN_SAMPLES:
        return None

    prices = [float(c) for c in closes]
    max_lag = min(20, len(prices) // 2)
    if max_lag < 2:
        return None

    log_lags: list[float] = []
    log_std: list[float] = []
    for lag in range(2, max_lag):
        diffs = [prices[i + lag] - prices[i] for i in range(len(prices) - lag)]
        if len(diffs) < 2:
            continue
        std = statistics.pstdev(diffs)
        if std <= 0:
            continue  # plaska cena na tym lagu - logarytm niezdefiniowany, pomijamy punkt
        log_lags.append(math.log(lag))
        log_std.append(math.log(std))

    if len(log_lags) < 2:
        return None

    hurst = _linear_regression_slope(log_lags, log_std)
    return Decimal(str(round(hurst, 4)))


def _regime_ok(hurst: Decimal | None) -> bool:
    """None (brak wystarczajacych danych) -> True, fail-open jak inne filtry."""
    if hurst is None:
        return True
    return hurst < HURST_MAX_FOR_ENTRY


def _spread_pct(quote: dict | None) -> Decimal | None:
    """
    Spread jako ulamek ceny srodkowej. quote to dict z bid/ask (patrz
    price_feed - patrz komentarz w evaluate_candidates o zrodle). None gdy
    brak bid/ask -> filtr sie nie stosuje (fail-open, liczone w EntryStats).
    """
    if not quote:
        return None
    bid = _d(quote.get("bid", 0) or 0)
    ask = _d(quote.get("ask", 0) or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


def _score(
    range_position: Decimal | None,
    trend: dict | None,
    spread_pct: Decimal | None,
    currency: str,
    max_spread_pct: Decimal,
) -> Decimal:
    """
    Composite score - wyzszy = lepszy kandydat. Skladniki znormalizowane do
    ~[0,1] i wazone stalymi W_* (patrz gora modulu). Brakujace dane dostaja
    wartosc neutralna (0.5) zamiast zerowej - kandydat bez swiec nie ma byc
    KARANY za awarie Finnhuba, ma byc po prostu "przecietny".
    """
    total = Decimal("0")

    # 1. Pozycja w zakresie dnia - im nizej, tym lepiej.
    total += W_RANGE_POSITION * (
        (Decimal("1") - range_position) if range_position is not None else Decimal("0.5")
    )

    # 2. Trend - preferujemy spokojny, lekko rosnacy. Idealny net to ~0..+2%,
    #    im dalej od tego (w obie strony), tym gorzej.
    if trend is not None:
        net = trend["net_pct"]
        ideal = Decimal("0.01")
        distance = abs(net - ideal)
        # 5 pkt proc. odchylki -> wynik 0; blizej -> blizej 1.
        calm = max(Decimal("0"), Decimal("1") - distance / Decimal("0.05"))
        total += W_TREND_CALM * calm
    else:
        total += W_TREND_CALM * Decimal("0.5")

    # 3. Kara walutowa - USD musi pokonac round-trip FX (0.3%) zanim cokolwiek
    #    zarobi. Skalujemy wzgledem 1% ruchu jako punktu odniesienia.
    if currency == "USD":
        penalty = FX_ROUND_TRIP_PCT / Decimal("0.01")  # 0.3
        total -= W_FX_PENALTY * penalty

    # 4. Spread - im wezszy wzgledem dopuszczalnego progu, tym lepiej.
    # UWAGA NA JEDNOSTKI (bug zlapany w testach 23.07): max_spread_pct z
    # formularza jest w PROCENTACH (0.5 = 0.5%), a _spread_pct zwraca UŁAMEK
    # (0.005). Pierwsza wersja tej funkcji porownywala je wprost, przez co
    # szeroki spread 0.4% dostawal praktycznie ten sam score co waski 0.05%
    # (roznica 0.0035 zamiast 0.35) - skladnik byl de facto martwy.
    if spread_pct is not None and max_spread_pct > 0:
        max_spread_fraction = max_spread_pct / Decimal("100")
        ratio = min(spread_pct / max_spread_fraction, Decimal("1"))
        total += W_SPREAD * (Decimal("1") - ratio)
    else:
        total += W_SPREAD * Decimal("0.5")

    return total


def evaluate_candidate(
    asset,
    candles: list[dict] | None,
    settings: RiskSettings,
    stats: EntryStats,
    current_price: Decimal | None = None,
    quote: dict | None = None,
) -> Decimal | None:
    """
    Ocenia POJEDYNCZEGO kandydata. Zwraca score (wyzszy=lepszy) gdy przeszedl
    twarde filtry, albo None gdy odrzucony.

    KLUCZOWE DLA RATE LIMITU: `current_price` jest OPCJONALNE. Gdy None,
    uzywamy ceny zamkniecia ostatniej swiecy jako przyblizenia. Dzieki temu
    ocena WSZYSTKICH kandydatow (moze ich byc 38) nie kosztuje ANI JEDNEGO
    zapytania o cene - swiece i tak sa juz w cache (30 min, wspoldzielone z
    filtrem trendu i ATR w bot_engine). Zywa cena jest pobierana DOPIERO dla
    zwyciezcy, wewnatrz _enter_position - czyli 1 zapytanie na tick, tyle
    samo co przed wprowadzeniem scoringu.

    Cena ze swiecy moze byc do 30 min stara, ale pozycja w zakresie dnia i
    tak jest wskaznikiem zgrubnym ("gora czy dol dzisiejszej sesji"), nie
    wymaga precyzji co do ticku.

    `candles` - z price_feed.get_mini_chart_ohlc. Gdy HURST_FILTER_ENABLED,
    wywolujacy przekazuje DLUZSZE okno (HURST_LOOKBACK_DAYS) - _range_position
    i _trend_metrics licza sie WYLACZNIE z ostatnich RECENT_WINDOW_ROWS z tej
    listy (no-op przy dzisiejszym krotszym fetchu), a Hurst z calej listy.
    `quote` - opcjonalny dict z bid/ask; None gdy zrodlo niedostepne.
    """
    stats.considered += 1

    if not candles:
        stats.skipped_no_candles += 1

    if current_price is None and candles:
        current_price = _d(candles[-1].get("c", 0))
    if current_price is None:
        current_price = Decimal("0")

    recent_candles = candles[-RECENT_WINDOW_ROWS:] if candles else candles

    # --- Twardy filtr: pozycja w zakresie dnia ---
    range_position = _range_position(recent_candles, current_price)
    if range_position is not None and range_position > MAX_ENTRY_RANGE_POSITION:
        stats.rejected_range += 1
        return None

    # --- Twardy filtr: trend dwustronny ---
    trend = _trend_metrics(recent_candles)
    if not _trend_ok(trend):
        stats.rejected_trend += 1
        return None

    # --- Twardy filtr: regime (Hurst Exponent), patrz docstring modulu ---
    if HURST_FILTER_ENABLED:
        closes = [_d(c.get("c", 0)) for c in candles if _d(c.get("c", 0)) > 0]
        hurst = compute_hurst_exponent(closes)
        if hurst is None:
            stats.skipped_no_regime_data += 1
        elif not _regime_ok(hurst):
            stats.rejected_regime += 1
            return None

    # --- Twardy filtr: spread ---
    spread_pct = _spread_pct(quote)
    if spread_pct is None:
        stats.skipped_no_spread_data += 1
    else:
        max_spread = _d(settings.max_spread_pct or 0)
        # max_spread_pct w formularzu jest w PROCENTACH (np. 0.5 = 0.5%),
        # a _spread_pct zwraca ulamek (0.005) - stad /100.
        if max_spread > 0 and spread_pct > max_spread / Decimal("100"):
            stats.rejected_spread += 1
            return None

    stats.passed += 1
    return _score(range_position, trend, spread_pct, asset.currency, _d(settings.max_spread_pct or 0))


def rank_candidates(assets, settings, candles_getter, quote_getter=None) -> tuple[list, EntryStats]:
    """
    Ocenia WSZYSTKICH kandydatow i zwraca (posortowana lista [(asset, score)],
    statystyki). Najlepszy pierwszy.

    To zastepuje dawne "wez pierwszego z BotAsset.query...all()" - do 23.07 o
    tym ktore 10 z 38 aktywow zostanie otwarte decydowala kolejnosc wierszy
    w bazie (brak order_by), a nie jakiekolwiek kryterium rynkowe.

    candles_getter: callable(ticker) -> list[dict] | None
    quote_getter:   callable(ticker) -> dict|None (bid/ask), opcjonalny -
                    gdy None, filtr spreadu sie nie stosuje (fail-open,
                    zliczane w EntryStats.skipped_no_spread_data).
    """
    stats = EntryStats()
    scored = []

    for asset in assets:
        candles = candles_getter(asset.ticker)
        quote = quote_getter(asset.ticker) if quote_getter is not None else None
        score = evaluate_candidate(asset, candles, settings, stats, quote=quote)
        if score is not None:
            scored.append((asset, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored, stats


# =========================================================================
# Bezpiecznik dziennej straty (max_daily_loss)
# =========================================================================
# Do 23.07 max_daily_loss bylo MARTWYM POLEM FORMULARZA - zaden kod go nie
# czytal (przyznane wprost w docstringu bot_engine). Bot mial DCA do N
# poziomow, do 10 rownoczesnych pozycji i zero dzialajacego bezpiecznika.
#
# UWAGA - SWIADOME UPROSZCZENIE: sumujemy P&L z pozycji EUR i USD BEZ
# przewalutowania (nie mamy kursu EUR/USD w appce, a dociaganie go tylko po
# to byloby kolejna zaleznoscia zewnetrzna). Przy mikro-kwotach (1-25 na
# pozycje) blad jest rzedu kilku procent progu, wiec bezpiecznik zadziala
# odrobine za wczesnie albo za pozno - to akceptowalne dla mechanizmu
# ktorego zadaniem jest "zatrzymaj sie gdy dzien idzie zle", nie ksiegowosc.

def _start_of_day_utc() -> dt.datetime:
    now = dt.datetime.utcnow()
    return dt.datetime(now.year, now.month, now.day)


def compute_today_pnl(user_id: int, live_price_getter, settings: RiskSettings | None = None) -> dict:
    """
    Zwraca {"realized", "unrealized", "total", "unpriced"} za DZISIAJ (UTC).

    realized: pozycje CLOSED z closed_at >= poczatek dnia UTC i znanym
    close_price (starsze pozycje sprzed 2026-07-21 maja NULL - pomijane,
    liczone w "unpriced" zeby bylo widac ze suma jest niepelna).
    unrealized: WSZYSTKIE aktualnie otwarte (to stan "teraz", nie zdarzenie
    z okna czasowego) wyceniane zywa cena.

    live_price_getter: callable(ticker) -> Decimal|None. Wstrzykiwane przez
    bot_engine (ma juz komplet kluczy API do price_feed) zeby ten modul nie
    musial znac konfiguracji.

    `settings` (Adam, 2026-08-11: "otwiera i zamyka po API niech dolicza FX,
    bo inaczej będę robił za darmo") - bez tego check_daily_loss_limit()
    ponizej liczyl dzisiejszy P&L z surowej ceny instrumentu, ignorujac
    ~0.15%/noge koszt przewalutowania na STOP-ach silnika przez API (patrz
    RiskSettings.fx_cost_adjustment_enabled/fx_fee_pct) - realna dzienna
    strata mogla przekroczyc max_daily_loss zanim licznik to zauwazyl.
    """
    cutoff = _start_of_day_utc()
    env = current_environment(user_id)

    realized = Decimal("0")
    unpriced = 0
    closed = (
        ActiveTrade.query
        .filter_by(user_id=user_id, is_paper=False, status="CLOSED", environment=env)
        .filter(ActiveTrade.closed_at >= cutoff)
        .all()
    )
    for trade in closed:
        if trade.close_price is None:
            unpriced += 1
            continue
        cost_basis = fx_adjusted_cost_basis(trade.buy_price, trade.currency, settings, trade.ticker, trade.buy_order_id)
        realized += (trade.close_price - cost_basis) * trade.quantity

    unrealized = Decimal("0")
    open_trades = ActiveTrade.query.filter_by(user_id=user_id, is_paper=False, status="OPEN", environment=env).all()
    for trade in open_trades:
        price = live_price_getter(trade.ticker)
        if price is None or price <= 0:
            unpriced += 1
            continue
        cost_basis = fx_adjusted_cost_basis(trade.average_price, trade.currency, settings, trade.ticker, trade.buy_order_id)
        unrealized += (price - cost_basis) * trade.quantity

    return {
        "realized": realized,
        "unrealized": unrealized,
        "total": realized + unrealized,
        "unpriced": unpriced,
    }


def check_daily_loss_limit(user_id: int, settings: RiskSettings, live_price_getter) -> dict | None:
    """
    Sprawdza czy laczna dzisiejsza strata (zrealizowana + niezrealizowana)
    przekroczyla RiskSettings.max_daily_loss. Jesli TAK - ustawia
    is_bot_active=False i zwraca podsumowanie (wywolujacy loguje i wylacza
    poswiadczenia). Jesli nie - zwraca None.

    Prog traktujemy jako wartosc DODATNIA oznaczajaca dopuszczalna strate
    (tak jest w formularzu: "max_daily_loss = 10.0"), wiec porownujemy z
    -max_daily_loss.
    """
    limit = _d(settings.max_daily_loss or 0)
    if limit <= 0:
        return None  # brak skonfigurowanego limitu - nic nie egzekwujemy

    pnl = compute_today_pnl(user_id, live_price_getter, settings)
    if pnl["total"] > -limit:
        return None

    settings.is_bot_active = False
    db.session.commit()
    return pnl
