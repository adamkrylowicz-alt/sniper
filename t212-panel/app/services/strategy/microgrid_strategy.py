"""
app/services/strategy/microgrid_strategy.py
=============================================
Czysta logika decyzyjna Micro-Grid Bota (progi/trailing STOP, sizing wejścia),
WYDZIELONA 2026-07-28 z `bot_engine.py` - druga część event-driven refaktoru
(pierwsza: `signal_strategy.py`, pilot na Sygnale). Zero importów
T212Client/db/Flask - tylko Decimal in, Decimal/dataclass/int out.

Wydzielone WYŁĄCZNIE: sizing wejścia (`_enter_position`) i matematyka
ciągłego trailing STOP (`_manage_trailing_exit`, Faza 1). CELOWO nie
obejmuje CAŁEGO `_trigger_dca_buys`/`_process_entries` (te zostają w
`bot_engine.py`) - WYJĄTEK dodany 2026-07-31 (patrz niżej): sam detektor
"szoku" używany PRZEZ `_trigger_dca_buys`, bo to czysta, testowalna
matematyka identyczna z resztą tego modułu.

Pełne uzasadnienie każdego kroku matematyki (dlaczego floor liczony TYLKO
przy pierwszym uzbrojeniu, dlaczego floor_anchor=max(ref_price,current_price)
a nie goły ref_price, dlaczego max(floor, ciasny_target) zamiast samego
floora) zostaje w docstringu `_manage_trailing_exit()` w bot_engine.py -
tu tylko sama, już zweryfikowana formuła.

--- Detektor "szoku" (zmiany reżimu), dodany 2026-07-31 -------------------
Po lekturze serii "Build Better Strategies" (financial-hacker.com, cz.1) -
historia pułapu SNB/CHF: siatka DCA zakłada powrót do średniej, ale gdy
instrument doświadcza NAGŁEGO, nieciągłego ruchu (wyniki finansowe, news,
delisting) zamiast zwykłego dryfu, dalsze dokupowanie w dół to nie "łapanie
okazji" tylko dokładanie kapitału do zdarzenia, które może się NIE odwrócić.
`is_shock()` odróżnia "gwałtowny, niedawny ruch" (odrzuć tę nogę DCA, ochrona
zostaje wyłącznie przez trailing-stop/exhausted-DCA-floor) od zwykłego,
stopniowego dryfu w dół (dla którego grid DCA jest zaprojektowany).
DOMYŚLNIE WYŁĄCZONY (SHOCK_FILTER_ENABLED=False) - nowa logika wpływająca
na realne dokupywanie bota wymaga backtestu PRZED włączeniem, zgodnie z
zasadą "nigdy nie zgaduj nowego parametru ryzyka".

--- Money management: skalowanie √equity, dodane 2026-07-31 ---------------
Po lekturze Części 3 serii - ostrzeżenie przed LINIOWYM skalowaniem wielkości
pozycji z kapitałem (drawdown rośnie jak √T, może pochłonąć konto). Zamiast
tego `compute_equity_scaled_amount()` skaluje efektywną kwotę wejścia/DCA
PIERWIASTKIEM stosunku bieżącego equity do equity odniesienia (baseline,
ustawianego w RiskSettings przy włączeniu funkcji) - wolniejszy wzrost niż
%equity, ale kapitał nie stoi w miejscu gdy konto rośnie. Kelly/OptimalF
(też wspomniane w artykule) świadomie ODRZUCONE na razie - baza ma za mało/
za zaszumionych danych (41 zamkniętych transakcji, 9 dni historii, 56% bez
znanego close_price) na wiarygodne oszacowanie edge'a bota.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class EntryValidationError(Exception):
    """Wejście odrzucone (wyliczona ilość <= 0) - treść już gotowa do zalogowania."""


@dataclass(frozen=True)
class EntryDecision:
    quantity: Decimal


def compute_entry_quantity(entry_amount: Decimal, price: Decimal) -> EntryDecision:
    """Wyciągnięte z `_enter_position()` w bot_engine.py."""
    quantity = (entry_amount / price).quantize(Decimal("0.0001"))
    if quantity <= 0:
        raise EntryValidationError(f"wyliczona ilość <= 0 (kwota {entry_amount} / cena {price}).")
    return EntryDecision(quantity=quantity)


def compute_milestone_steps(ref_price: Decimal, current_price: Decimal, step: Decimal) -> int:
    """Ile PEŁNYCH progów `step` minęła cena powyżej ref_price (0 gdy na minusie/płasko)."""
    profit_pct = (current_price - ref_price) / ref_price
    return int(profit_pct / step) if profit_pct > 0 else 0


def compute_exhausted_dca_floor(
    current_price: Decimal,
    atr_distance: Decimal | None,
    stop_loss_pct: Decimal,
) -> Decimal:
    """
    Dodane 2026-07-28 - ostatnia linia obrony dla pozycji, która wyczerpała
    WSZYSTKIE poziomy DCA (`dca_level == max_dca_levels-1`) a cena wciąż
    siedzi poniżej average_price (`compute_milestone_steps` < 2, więc
    normalny warunek uzbrojenia w `_manage_trailing_exit` nigdy się nie
    spełni - do tej zmiany taka pozycja zostawała UZBROJONA-NIGDY, bez
    żadnego stopu, dopóki cena sama nie wróci nad breakeven+2*step; znalezione
    backtestem 2026-07-28: PRXa_EQ/MCp_EQ/SAPd_EQ na dca_level=4, -27.7%/
    -18.1%/-13.7%, `stop_target_price=None`).

    Ta sama matematyka co floor przy PIERWSZYM uzbrojeniu (ATR*1.8 gdy
    dostępne, inaczej stop_loss_pct - patrz compute_trailing_stop), ale
    zakotwiczona w AKTUALNEJ cenie, nie w ref_price/average - average jest
    już wysoko nad nami (stąd brak amunicji), więc floor liczony od niej
    wypadłby jeszcze wyżej niż obecna cena i wykonałby się od razu.
    Zakotwiczenie w current_price daje realny, sensowny bufor OD TEGO
    miejsca w dół, zamiast próbować odtworzyć nieosiągalny już breakeven.

    Zwraca ZAWSZE realną cenę (nigdy None) - w odróżnieniu od
    compute_trailing_stop przy już uzbrojonej pozycji, tu nie ma czego
    porównywać (brak poprzedniego stopu), więc każdy wynik jest z definicji
    poprawą względem braku ochrony.
    """
    if atr_distance is not None:
        return (current_price - atr_distance).quantize(Decimal("0.0001"))
    return (current_price * (1 - stop_loss_pct)).quantize(Decimal("0.0001"))


def compute_trailing_stop(
    is_first_arm: bool,
    ref_price: Decimal,
    current_price: Decimal,
    step: Decimal,
    existing_stop_target: Decimal | None,
    atr_distance: Decimal | None,
    stop_loss_pct: Decimal,
    min_requote_fraction: Decimal,
) -> Decimal | None:
    """
    Wyciągnięte z `_manage_trailing_exit()` (Faza 1) w bot_engine.py.
    Zwraca nowy poziom STOP-a, albo `None` gdy nic do zrobienia (dla
    "już uzbrojony" - albo brak poprawy, albo poprawa za mała żeby
    opłacało się Cancel-Replace'em).

    `is_first_arm=True`: PIERWSZE uzbrojenie - `max(floor, ciasny_target_teraz)`,
    floor z ATR gdy dostępne, inaczej fallback na `stop_loss_pct`.

    `is_first_arm=False`: już uzbrojony - czysty ciągły trailing względem
    WŁASNEGO poprzedniego poziomu (nigdy w dół), z progiem min. requote
    (`existing_stop_target` MUSI być podane, nie może być `None`).
    """
    if is_first_arm:
        floor_anchor = max(ref_price, current_price)
        if atr_distance is not None:
            floor_candidate = (floor_anchor - atr_distance).quantize(Decimal("0.0001"))
        else:
            floor_candidate = (floor_anchor * (1 - stop_loss_pct)).quantize(Decimal("0.0001"))
        tight_target_now = (current_price * (1 - step)).quantize(Decimal("0.0001"))
        return max(floor_candidate, tight_target_now)

    assert existing_stop_target is not None, "already-armed trade must have a stop_target_price"
    continuous_target = (current_price * (1 - step)).quantize(Decimal("0.0001"))
    candidate_stop = max(existing_stop_target, continuous_target)

    min_requote_threshold = existing_stop_target * (1 + step * min_requote_fraction)
    if candidate_stop < min_requote_threshold:
        return None

    return candidate_stop


# =========================================================================
# Detektor "szoku" (zmiany reżimu) - patrz docstring modułu wyżej
# =========================================================================
SHOCK_FILTER_ENABLED = False   # patrz docstring modulu - domyslnie WYLACZONY
SHOCK_LOOKBACK_DAYS = 3        # ile ostatnich sesji sprawdzamy pod katem gwaltownego ruchu
SHOCK_ATR_MULTIPLIER = Decimal("3")  # 1-dniowy spadek > 3x ATR = anomalia, nie zwykly szum


def compute_max_recent_single_day_drop_pct(
    candles: list[dict], lookback_days: int = SHOCK_LOOKBACK_DAYS,
) -> Decimal | None:
    """
    Największy JEDNODNIOWY spadek (close[i-1]->close[i], jako dodatni ułamek)
    w ostatnich `lookback_days` sesjach. None gdy za mało świec do policzenia
    choćby jednej pary - wywołujący traktuje to jak brak danych (fail-open).

    `candles` - lista dictów {"o","h","l","c"}, najstarsza->najnowsza (ten sam
    format co price_feed.get_mini_chart_ohlc).
    """
    if not candles or len(candles) < 2:
        return None
    window = candles[-(lookback_days + 1):]
    if len(window) < 2:
        return None

    max_drop = Decimal("0")
    for i in range(1, len(window)):
        prev_close = Decimal(str(window[i - 1].get("c", 0)))
        cur_close = Decimal(str(window[i].get("c", 0)))
        if prev_close <= 0:
            continue
        drop = (prev_close - cur_close) / prev_close
        if drop > max_drop:
            max_drop = drop
    return max_drop


def is_shock(max_drop_pct: Decimal | None, atr_pct: Decimal | None) -> bool:
    """
    True gdy najwiekszy niedawny jednodniowy spadek WYRAZNIE przekracza
    typowa zmiennosc instrumentu (ATR jako ulamek ceny) - sygnal nieciaglego,
    gwaltownego ruchu (wyniki/news), nie zwyklego dryfu w dol ktory grid ma
    absorbowac. Brak danych (None po ktorejkolwiek stronie) -> False
    (fail-open, jak inne filtry w tym module przy braku danych).
    """
    if max_drop_pct is None or atr_pct is None or atr_pct <= 0:
        return False
    return max_drop_pct > atr_pct * SHOCK_ATR_MULTIPLIER


# =========================================================================
# Money management: skalowanie √equity (Build Better Strategies, część 3)
# =========================================================================
EQUITY_SCALING_MIN_MULTIPLIER = Decimal("0.5")  # ARBITRALNY bezpiecznik - patrz docstring nizej, NIE strojony parametr
EQUITY_SCALING_MAX_MULTIPLIER = Decimal("3")


def compute_equity_scaled_amount(
    base_amount: Decimal,
    current_equity: Decimal,
    baseline_equity: Decimal,
    min_multiplier: Decimal = EQUITY_SCALING_MIN_MULTIPLIER,
    max_multiplier: Decimal = EQUITY_SCALING_MAX_MULTIPLIER,
) -> Decimal:
    """
    Przeskalowanie `base_amount` (dziś: BotAsset.entry_amount) PIERWIASTKIEM
    stosunku bieżącego equity do equity odniesienia (baseline, zapisany w
    momencie włączenia RiskSettings.equity_sizing_enabled - patrz
    routes/bot.py::update_settings) - sqrt(current_equity/baseline_equity),
    NIE liniowo. Realizacja rady z serii "Build Better Strategies" (część 3,
    financial-hacker.com): drawdown przy liniowym skalowaniu wielkości
    pozycji rośnie jak √T, więc pierwiastkowe skalowanie WIELKOŚCI wejścia
    względem equity jest naturalną przeciwwagą - gdy equity rośnie N razy,
    kwota wejścia rośnie √N razy (WOLNIEJ niż proporcjonalnie), nie 1:1.

    current_equity == baseline_equity -> mnożnik=1, wynik DOKŁADNIE
    base_amount (przed zaokrągleniem) - brak zmiany względem stanu odniesienia.

    Guard baseline_equity<=0 (baseline nigdy nie ustawiony) ALBO
    current_equity<=0 (błędny/pusty odczyt equity) -> zwraca base_amount BEZ
    ŻADNEJ zmiany (fail-safe, identyczne zachowanie do wyłączonej flagi).

    Klamra [min_multiplier, max_multiplier] (domyślnie 0.5x-3x) to
    ARBITRALNY bezpiecznik przeciw pojedynczemu glitchowi/błędnemu odczytowi
    equity (np. chwilowo zwrócone totalValue=0 albo absurdalnie duże przez
    błąd API) dającemu groteskowo małą/dużą pozycję - NIE jest to strojony
    parametr ryzyka (celowo bez odpowiednika w RiskSettings/UI). W normalnym
    działaniu equity zmienia się stopniowo, klamra praktycznie nigdy nie
    powinna być aktywna - żeby dotknąć górnej granicy (3x) equity musiałoby
    urosnąć 9-krotnie względem baseline.
    """
    if baseline_equity <= 0 or current_equity <= 0:
        return base_amount
    ratio = current_equity / baseline_equity
    multiplier = ratio.sqrt()
    multiplier = max(min_multiplier, min(max_multiplier, multiplier))
    return (base_amount * multiplier).quantize(Decimal("0.01"))
