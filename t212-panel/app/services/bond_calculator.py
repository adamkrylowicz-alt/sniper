"""
app/services/bond_calculator.py
================================
Obliczenia dla portfolia obligacji.

Koncepcja: 80% akcje / 20% obligacje, rebalancing gdy drift > 1%.
Kupon YTD - prorata od rzeczywistego roku handlu.
Performance vs S&P500 (SPY) - benchmark niezależny.
"""

from decimal import Decimal
from datetime import datetime, date
from typing import Optional, List, Dict

from ..models import Instrument, UserSettings


# Lista obligacyjnych ETF-ów EUR do ręcznego wyboru (Adam, 2026-09-02: "lista
# tak jak są np. akcje w innych zakładkach, będę mógł sobie wybierać i kupować
# co chcę ręcznie") - wszystkie tickery zweryfikowane w bazie `instruments`
# (istnieją, currency_code="EUR"). Flagowe, duże/płynne fundusze głównych
# emitentów (iShares/Vanguard/Xtrackers), pogrupowane po kategorii ryzyka.
BOND_UNIVERSE = [
    {
        "ticker": "X15Ed_EQ", "name": "Xtrackers II Eurozone Government Bond 15-30 (Acc)", "category": "Rządowe (długie duration)",
        "description": "Obligacje rządowe strefy euro z zapadalnością 15-30 lat, wariant Acc (kupon reinwestowany automatycznie, nie wypłacany). Wyższy kupon niż fundusze \"core\" i większy potencjał wzrostu ceny gdy stopy procentowe spadają, ale też większe wahania w międzyczasie - pasuje do bardzo długiego horyzontu (10-20+ lat), gdzie nie musisz sprzedawać w złym momencie.",
    },
    {
        "ticker": "VGEAd_EQ", "name": "Vanguard EUR Eurozone Government Bond (Acc)", "category": "Rządowe",
        "description": "Szeroki rynek obligacji rządowych strefy euro, wszystkie zapadalności naraz (średnie duration), wariant Acc. Zwykle niższy koszt zarządzania (TER) niż odpowiedniki iShares/Xtrackers.",
    },
    {
        "ticker": "DBXNd_EQ", "name": "Xtrackers II Eurozone Government Bond (Acc)", "category": "Rządowe",
        "description": "Ten sam segment co VGEAd_EQ (szeroki rynek rządowy strefy euro), inny emitent ETF-u - do dywersyfikacji między emitentami.",
    },
    {
        "ticker": "IEAAl_EQ", "name": "iShares Core EUR Corp Bond (Acc)", "category": "Korporacyjne IG",
        "description": "Obligacje spółek z ratingiem inwestycyjnym (investment grade, BBB+ i wyżej) strefy euro. Wyższy kupon niż rządowe, ryzyko kredytowe nadal niskie. Wariant Acc - kupon reinwestowany.",
    },
    {
        "ticker": "VECAd_EQ", "name": "Vanguard EUR Corporate Bond (Acc)", "category": "Korporacyjne IG",
        "description": "Odpowiednik Vanguard tej samej strategii (korporacyjne investment grade EUR), zwykle niższy TER.",
    },
    {
        "ticker": "HIGHl_EQ", "name": "iShares EUR High Yield Corp Bond (Acc)", "category": "High Yield",
        "description": "Obligacje spółek PONIŻEJ ratingu inwestycyjnego (\"śmieciowe\", wyższe ryzyko bankructwa emitenta). Wyższy kupon niż IG, ale wyraźnie większa wrażliwość na spowolnienie gospodarcze - traktuj jako najbardziej ryzykowną kategorię tej listy.",
    },
    {
        "ticker": "XHYAd_EQ", "name": "Xtrackers II EUR High Yield Corporate Bond (Acc)", "category": "High Yield",
        "description": "Alternatywa od innego emitenta ETF-u dla tej samej kategorii (high yield EUR).",
    },
    {
        "ticker": "VAGFd_EQ", "name": "Vanguard Global Aggregate Bond (Acc)", "category": "Globalne",
        "description": "Bardzo szeroka dywersyfikacja - rządowe i korporacyjne obligacje z całego świata (USA, Europa, rynki wschodzące) w jednym funduszu, zabezpieczone walutowo do EUR (kurs walut nie wpływa na wynik).",
    },
    {
        "ticker": "EUNAd_EQ", "name": "iShares Core Global Aggregate Bond (Acc)", "category": "Globalne",
        "description": "Odpowiednik iShares tej samej strategii co VAGFd_EQ (globalna dywersyfikacja, EUR hedged).",
    },
    {
        "ticker": "IBCIa_EQ", "name": "iShares EUR Inflation Linked Govt Bond (Acc)", "category": "Inflacja",
        "description": "Kapitał i kupon rosną razem z inflacją strefy euro - ochrona siły nabywczej pieniędzy, kosztem niższego nominalnego zwrotu w okresach niskiej inflacji.",
    },
]

# Stopy kuponowe - ponytail: BRAK realnych, zweryfikowanych danych dla
# funduszy z BOND_UNIVERSE (celowo NIE zgadujemy/nie konfabulujemy liczb
# finansowych) - calculate_coupon_ytd() zwraca 0 dla tickerów spoza tego
# dict, dopóki Adam nie poda prawdziwej stopy z prospektu/KID danego ETF-u.
COUPON_YIELDS = {}

TARGET_ALLOCATION = {
    "stocks": Decimal("80"),
    "bonds": Decimal("20"),
}


def get_bond_tickers() -> List[str]:
    """Lista obligacyjnych ETF-ów do śledzenia (portfolio/historia)."""
    return [b["ticker"] for b in BOND_UNIVERSE]


def get_bond_portfolio(user_id: int, client) -> List[Dict]:
    """
    Pobiera pozycje obligacyjnych ETF-ów z bieżącego portfela T212.
    Zwraca listę słowników z ilością, średnią ceną, ceną bieżącą, wartością EUR.

    ponytail: gdy user będzie chciał inne ETF-y, dodać je do COUPON_YIELDS dict
    """
    bond_tickers = get_bond_tickers()
    portfolio = client.get_portfolio()

    result = []
    for position in portfolio:
        ticker = position.get("ticker")
        if ticker not in bond_tickers:
            continue

        result.append({
            "ticker": ticker,
            "quantity": Decimal(str(position.get("quantity", 0))),
            "avg_price": Decimal(str(position.get("averagePrice", 0))),
            "current_price": Decimal(str(position.get("currentPrice", 0))),
            "value": Decimal(str(position.get("quantity", 0))) * Decimal(str(position.get("currentPrice", 0))),
            "currency": "EUR",  # Wszystkie obligacyjne ETF-y w EUR
        })

    return result


def calculate_allocation(bonds_value: Decimal, all_portfolio_value: Decimal) -> Dict:
    """
    Liczy % alokacji akcji/obligacji na podstawie wartości EUR.
    Zwraca { stocks_pct, bonds_pct, target_stocks_pct, target_bonds_pct }
    """
    if all_portfolio_value <= 0:
        return {
            "stocks_pct": Decimal("0"),
            "bonds_pct": Decimal("0"),
            "target_stocks_pct": TARGET_ALLOCATION["stocks"],
            "target_bonds_pct": TARGET_ALLOCATION["bonds"],
        }

    bonds_pct = (bonds_value / all_portfolio_value * Decimal("100")).quantize(Decimal("0.1"))
    stocks_pct = (Decimal("100") - bonds_pct).quantize(Decimal("0.1"))

    return {
        "stocks_pct": stocks_pct,
        "bonds_pct": bonds_pct,
        "target_stocks_pct": TARGET_ALLOCATION["stocks"],
        "target_bonds_pct": TARGET_ALLOCATION["bonds"],
    }


def calculate_coupon_ytd(bond_portfolio: List[Dict], year_start_date: Optional[date] = None) -> Dict:
    """
    Liczy kupon YTD na podstawie twardego kodu COUPON_YIELDS.
    year_start_date=None (domyślnie) - od 1 stycznia bieżącego roku.
    Zwraca { eur_amount, pct_of_annual }
    """
    if year_start_date is None:
        now = datetime.utcnow()
        year_start_date = date(now.year, 1, 1)

    today = date.today()
    days_elapsed = (today - year_start_date).days + 1  # +1 żeby liczyć dzisiaj

    if days_elapsed <= 0:
        return {"eur_amount": Decimal("0"), "pct_of_annual": Decimal("0")}

    total_coupon = Decimal("0")
    for position in bond_portfolio:
        ticker = position["ticker"]
        annual_yield = COUPON_YIELDS.get(ticker, Decimal("0"))
        quantity = position["quantity"]
        current_price = position["current_price"]

        # Uproszczenie: kupon liczymy jak procent wartości bieżącej
        # (nie wartości zakupu, bo ETF-y handlują każdego dnia)
        position_value = quantity * current_price
        annual_coupon = position_value * annual_yield / Decimal("100")
        proportional_coupon = annual_coupon * Decimal(days_elapsed) / Decimal("365")
        total_coupon += proportional_coupon

    # Procent YTD vs całoroczna stopa
    total_annual = Decimal("0")
    for position in bond_portfolio:
        ticker = position["ticker"]
        annual_yield = COUPON_YIELDS.get(ticker, Decimal("0"))
        total_annual += position["value"] * annual_yield / Decimal("100")

    ytd_pct = (Decimal(days_elapsed) / Decimal("365") * Decimal("100")).quantize(Decimal("0.1"))

    return {
        "eur_amount": total_coupon.quantize(Decimal("0.01")),
        "pct_of_annual": ytd_pct,
    }


def calculate_performance_vs_spy(
    bond_portfolio: List[Dict],
    spy_price_now: Decimal,
    spy_price_start: Decimal,
    lookback_days: int = 30,
) -> Dict:
    """
    Liczy return obligacji vs S&P500 w okresie lookback_days.
    spy_price_start - cena SPY lookback_days temu (albo przy zakupie obligacji)
    Zwraca { bonds_return_pct, spy_return_pct, outperformance_pct }
    """
    if not bond_portfolio:
        spy_return_pct = Decimal("0")
        if spy_price_start and spy_price_start > 0:
            spy_return_pct = ((spy_price_now - spy_price_start) / spy_price_start * Decimal("100")).quantize(Decimal("0.1"))
        return {
            "bonds_return_pct": Decimal("0"),
            "spy_return_pct": spy_return_pct,
            "outperformance_pct": Decimal("0") - spy_return_pct,
        }

    # Średni return obligacji (weighted by current value)
    total_bonds_value = sum(p["value"] for p in bond_portfolio)
    bonds_return_pct = Decimal("0")

    if total_bonds_value > 0:
        for position in bond_portfolio:
            weight = position["value"] / total_bonds_value
            # Uproszczenie: return = (current - avg) / avg * 100
            if position["avg_price"] and position["avg_price"] > 0:
                position_return = ((position["current_price"] - position["avg_price"]) / position["avg_price"] * Decimal("100"))
                bonds_return_pct += position_return * weight

    spy_return_pct = Decimal("0")
    if spy_price_start and spy_price_start > 0:
        spy_return_pct = ((spy_price_now - spy_price_start) / spy_price_start * Decimal("100")).quantize(Decimal("0.1"))

    bonds_return_pct = bonds_return_pct.quantize(Decimal("0.1"))

    return {
        "bonds_return_pct": bonds_return_pct,
        "spy_return_pct": spy_return_pct,
        "outperformance_pct": (bonds_return_pct - spy_return_pct).quantize(Decimal("0.1")),
    }


def get_rebalancing_suggestion(
    current_allocation: Dict,
    total_portfolio_value: Decimal,
    drift_threshold_pct: Decimal = Decimal("1.0"),
) -> Optional[Dict]:
    """
    Jeśli alokacja obligacji dryftuje >drift_threshold_pct od celu, zwraca
    dict {text, ticker, amount_eur} (zawsze proponuje VGOV jako bazowy ETF) -
    strukturalne pola żeby wołający (JS przez /kup) nie musiał parsować
    tekstu. None = alokacja w porządku.
    """
    bonds_pct = current_allocation.get("bonds_pct", Decimal("0"))
    target_bonds_pct = current_allocation.get("target_bonds_pct", Decimal("0"))

    drift = abs(bonds_pct - target_bonds_pct)

    if drift < drift_threshold_pct:
        return None

    if bonds_pct < target_bonds_pct:
        # Trzeba kupić obligacje
        bonds_value_needed = total_portfolio_value * target_bonds_pct / Decimal("100")
        bonds_value_current = total_portfolio_value * bonds_pct / Decimal("100")
        buy_amount = (bonds_value_needed - bonds_value_current).quantize(Decimal("0.01"))
        return {
            "text": f"Kup {buy_amount}€ VGOV żeby wrócić do 80/20",
            "ticker": "VGOV",
            "amount_eur": buy_amount,
        }
    else:
        # Trzeba sprzedać obligacje (rzadsze, ale logicznie poprawne)
        return None  # Dla wersji 1 pomijamy sugesty sprzedaży
