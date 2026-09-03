"""
app/routes/obligacje.py
=======================
Dashboard obligacji EUR (VGOV/IERC/IHYE) - alokacja 80/20, kupon YTD, rebalancing.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, render_template
from decimal import ROUND_UP, Decimal, InvalidOperation
import logging

logger = logging.getLogger(__name__)

from ..utils import login_required, current_user_id, current_master_key
from ..services.bond_calculator import (
    get_bond_portfolio,
    get_bond_tickers,
    calculate_allocation,
    calculate_coupon_ytd,
    calculate_performance_vs_spy,
    get_rebalancing_suggestion,
    BOND_UNIVERSE,
)
from ..services.bond_alerts import (
    check_rebalancing_needed,
    check_coupon_rate,
    check_daily_loss,
)
from ..models import YahooSymbolMap
from ..services import price_feed
from ..services.market_data_keys import get_decrypted_market_data_keys
from ..services.bot_engine import _required_precision
from ..services.t212_client import T212APIError
from .scalping import _fetch_portfolio_live, _log_order, _get_client

obligacje_bp = Blueprint("obligacje", __name__, url_prefix="/obligacje")

# Benchmark do porównania performance obligacji - SPDR S&P 500 UCITS ETF (Acc),
# EUR, prawdziwy T212 ticker (zweryfikowany w bazie instruments 2026-09-02).
SPY_TICKER = "SPYLa_EQ"


def _get_live_price(user_id: int, ticker: str) -> Decimal | None:
    """
    Cena "na żywo" dla jednego tickera - TEN SAM price_feed.get_live_price()
    co bot (IBKR/Alpaca/Finnhub -> Yahoo fallback, patrz price_feed.py).

    Naprawia bug 2026-09-03: poprzednia wersja wymagała TWARDO zapisanego
    klucza Finnhub (ApiKeySet.finnhub_key - pole które w ogóle nie istnieje
    na tym modelu, zawsze rzucało wyjątek i cicho zwracało None) zamiast
    korzystać z tego samego wielo-źródłowego fallbacku co reszta appki -
    "Kup" zawsze kończyło się błędem "brak ceny", myląco sugerując zły
    ticker T212, mimo że te ETF-y są jak najbardziej prawdziwe.
    """
    market_keys = get_decrypted_market_data_keys(user_id, current_master_key())
    return price_feed.get_live_price(
        market_keys.get("finnhub_api_key"),
        ticker,
        market_keys.get("alpaca_api_key"),
        market_keys.get("alpaca_api_secret"),
        market_keys.get("ibkr_host"),
        market_keys.get("ibkr_port"),
    )


def _is_already_resolved(ticker: str) -> bool:
    """
    True gdy ticker ma już (pozytywny lub negatywny) wpis w YahooSymbolMap -
    czyli _get_live_price() dla niego NIE odpali nowego yahoo_resolver.resolve()
    (a więc i ewentualnego powiadomienia Telegram, patrz yahoo_resolver.py).

    Używane w universe() - bug 2026-09-03 (Adam: "IHYEl_EQ CZEGO TEN SKORO JA
    KUPOWALEM X15Ed_EQ"): samo WEJŚCIE na stronę obligacji odpalało resolve()
    dla WSZYSTKICH 15 funduszy naraz (universe() woła _get_live_price w pętli
    po całym BOND_UNIVERSE), więc user dostawał na Telegramie prośby o
    potwierdzenie funduszy, których w ogóle jeszcze nie próbował kupić -
    myląco wyglądające jak efekt jego kliknięcia "Kup" na zupełnie innym
    funduszu. kup() (faktyczna, świadoma próba zakupu) nadal wywołuje
    _get_live_price bez tego ograniczenia - tam nowy resolve+Telegram jest
    pożądany.
    """
    return YahooSymbolMap.query.get(ticker) is not None


@obligacje_bp.route("/status", methods=["GET"])
@login_required
def status():
    """GET /obligacje/status - Portfolio allocation, kupon YTD, kursy live, performance vs SPY."""
    user_id = current_user_id()
    try:
        # Portfolio obligacji z T212
        client = _get_client()
        bond_portfolio = get_bond_portfolio(user_id, client)

        # Całość portfela (dla allocation %)
        all_portfolio = _fetch_portfolio_live(user_id)
        total_portfolio_value = Decimal(str(all_portfolio.get("total_value", 0)))

        bonds_value = sum(Decimal(str(pos.get("value", 0))) for pos in bond_portfolio)

        # Alokacja
        allocation = calculate_allocation(bonds_value, total_portfolio_value)

        # Kupon YTD
        coupon = calculate_coupon_ytd(bond_portfolio)

        # Cena SPY i performance
        spy_price = _get_live_price(user_id, SPY_TICKER)
        performance = {
            "bonds_return_pct": Decimal("0"),
            "spy_return_pct": Decimal("0"),
            "outperformance_pct": Decimal("0"),
        }

        if spy_price is not None:
            # Uproszczenie: punkt startowy = bieżąca cena * (1 - dzienna zmiana%
            # ekstrapolowana na 30 dni) - price_feed nie daje historii, tylko
            # bieżącą cenę - to i tak tylko orientacyjny wskaźnik do sekcji
            # Performance, nie księgowa wartość.
            performance = calculate_performance_vs_spy(
                bond_portfolio,
                spy_price_now=spy_price,
                spy_price_start=spy_price * Decimal("0.98"),  # Szacunek
                lookback_days=30,
            )

        # Sugestia rebalancingu - pokazujemy TYLKO gdy proponowany ticker ma
        # realną, żywą cenę (czyli faktycznie da się go kupić przez /kup).
        # Bez tego sekcja sugerowała zakup "VGOV" (placeholder bez prawdziwego
        # T212 tickera, patrz kursy()) i przycisk zawsze kończył się błędem -
        # myląca sekcja lepiej ukryta niż pokazana z gwarantowanym failem.
        rebalancing = get_rebalancing_suggestion(allocation, total_portfolio_value)
        if rebalancing:
            if _get_live_price(user_id, rebalancing["ticker"]) is None:
                rebalancing = None
            else:
                rebalancing = {**rebalancing, "amount_eur": float(rebalancing["amount_eur"])}

        response = {
            "allocation": {
                "stocks_pct": float(allocation.get("stocks_pct", 0)),
                "bonds_pct": float(allocation.get("bonds_pct", 0)),
                "target_stocks_pct": float(allocation.get("target_stocks_pct", 0)),
                "target_bonds_pct": float(allocation.get("target_bonds_pct", 0)),
            },
            "portfolio": [
                {
                    "ticker": p["ticker"],
                    "quantity": float(p["quantity"]),
                    "avg_price": float(p["avg_price"]),
                    "current_price": float(p["current_price"]),
                    "value_eur": float(p["value"]),
                    "pct": float((p["value"] / total_portfolio_value * Decimal("100")).quantize(Decimal("0.1"))) if total_portfolio_value > 0 else 0,
                }
                for p in bond_portfolio
            ],
            "coupon_ytd_eur": float(coupon.get("eur_amount", 0)),
            "coupon_ytd_pct": float(coupon.get("pct_of_annual", 0)),
            "performance_30d": {
                "bonds_return_pct": float(performance.get("bonds_return_pct", 0)),
                "spy_return_pct": float(performance.get("spy_return_pct", 0)),
                "outperformance_pct": float(performance.get("outperformance_pct", 0)),
            },
            "rebalancing_suggestion": rebalancing,
            "last_refresh_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }

        # Alerty (rebalancing/coupon/daily-loss) CELOWO WYŁĄCZONE (2026-09-02) -
        # Adam nie ma jeszcze kupionych obligacji, więc bonds_pct=0% zawsze
        # dryfuje od celu 20% i coupon_ytd zawsze jest poniżej pro-rata targetu
        # -> alert wysyłany przy KAŻDYM odświeżeniu strony (co 30s), spam na
        # Telegramie. Odkomentować gdy portfel obligacji faktycznie istnieje.
        # check_rebalancing_needed(user_id, allocation.get("bonds_pct", Decimal("0")))
        # check_coupon_rate(user_id, coupon.get("eur_amount", Decimal("0")))
        # check_daily_loss(user_id, bonds_value, bonds_value)

        return jsonify(response)
    except Exception as e:
        logger.error(f"[obligacje] /status error: {e}")
        return jsonify({"error": str(e)}), 500


@obligacje_bp.route("/kursy", methods=["GET"])
@login_required
def kursy():
    """GET /obligacje/kursy - Live price dla SPY (benchmark performance)."""
    user_id = current_user_id()
    try:
        quotes = {}
        price = _get_live_price(user_id, SPY_TICKER)
        if price is not None:
            quotes[SPY_TICKER] = {"price": float(price), "change_pct": None}

        return jsonify(quotes)
    except Exception as e:
        logger.error(f"[obligacje] /kursy error: {e}")
        return jsonify({"error": str(e)}), 500


@obligacje_bp.route("/universe", methods=["GET"])
@login_required
def universe():
    """
    GET /obligacje/universe - Lista wszystkich śledzonych obligacyjnych
    ETF-ów (BOND_UNIVERSE) z żywą ceną - do ręcznego przeglądania/kupowania
    (Adam, 2026-09-02: "lista tak jak są np. akcje w innych zakładkach").
    """
    user_id = current_user_id()
    try:
        result = []
        for bond in BOND_UNIVERSE:
            ticker = bond["ticker"]
            price = _get_live_price(user_id, ticker) if _is_already_resolved(ticker) else None
            result.append({
                "ticker": bond["ticker"],
                "name": bond["name"],
                "category": bond["category"],
                "price": float(price) if price is not None else None,
                "change_pct": None,
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"[obligacje] /universe error: {e}")
        return jsonify({"error": str(e)}), 500


@obligacje_bp.route("/kup", methods=["POST"])
@login_required
def kup():
    """
    POST /obligacje/kup - Buy bonds (Market order), log to OrderLog.

    Przyjmuje `amount_eur` (kwota, jak w sugestii rebalancingu) - ilość
    sztuk liczona tutaj z żywej ceny, żeby JS nie musiał jej znać. Jeśli
    żadne źródło (IBKR/Alpaca/Finnhub/Yahoo, patrz price_feed.get_live_price)
    nie ma ceny dla tickera, user dostaje czytelny błąd zamiast cichego
    "nic się nie dzieje".
    """
    user_id = current_user_id()
    try:
        data = request.json or {}
        ticker = data.get("ticker", "VGOV")
        raw_amount = data.get("amount_eur", 0)

        try:
            amount_eur = Decimal(str(raw_amount))
        except (InvalidOperation, ValueError):
            return jsonify({"error": "Nieprawidłowa kwota."}), 400
        if amount_eur <= 0:
            return jsonify({"error": "Nieprawidłowa kwota."}), 400

        price = _get_live_price(user_id, ticker)
        if price is None:
            return jsonify({
                "error": f"Nie można pobrać ceny dla '{ticker}' - brak danych we "
                         f"wszystkich źródłach (IBKR/Alpaca/Finnhub/Yahoo)."
            }), 400

        quantity = (amount_eur / price).quantize(Decimal("0.0001"))
        if quantity <= 0:
            return jsonify({"error": "Wyliczona ilość <= 0."}), 400

        client = _get_client()

        try:
            result = client.place_market_order(ticker, quantity)
        except T212APIError as exc:
            # Niektóre fundusze (potwierdzone na żywo: VGEAd_EQ) wymagają
            # mniejszej precyzji ilości niż standardowe 4 miejsca po
            # przecinku - ten sam błąd/fallback co boty, patrz
            # bot_engine.py::_place_buy_with_precision_fallback (tu Market,
            # nie Limit, więc logika retry powtórzona zamiast reużyta wprost).
            precision = _required_precision(exc)
            if precision is not None:
                adjusted = quantity.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_UP)
                if adjusted > 0 and adjusted != quantity:
                    try:
                        result = client.place_market_order(ticker, adjusted)
                        quantity = adjusted
                    except T212APIError as exc2:
                        exc = exc2
                        result = None
                else:
                    result = None
            else:
                result = None

            if result is None:
                _log_order(
                    user_id=user_id, ticker=ticker, side="buy", quantity=quantity,
                    price_snapshot=price, status="rejected",
                    block_reason=f"T212_ERROR_{exc.status_code}",
                )
                return jsonify({"error": str(exc)}), 502

        _log_order(
            user_id=user_id, ticker=ticker, side="buy", quantity=quantity,
            price_snapshot=price, status="sent", t212_order_id=result.order_id,
        )

        logger.info(f"[obligacje] Bought {quantity} {ticker} for user {user_id}")
        return jsonify({"order_id": result.order_id, "status": "success"}), 201
    except Exception as e:
        logger.error(f"[obligacje] /kup error: {e}")
        return jsonify({"error": str(e)}), 500


@obligacje_bp.route("/historia", methods=["GET"])
@login_required
def historia():
    """GET /obligacje/historia - OrderLog history dla śledzonych obligacyjnych ETF-ów."""
    from ..models import OrderLog

    user_id = current_user_id()
    try:
        bond_tickers = get_bond_tickers()
        orders = OrderLog.query.filter(
            OrderLog.user_id == user_id,
            OrderLog.ticker.in_(bond_tickers),
        ).order_by(OrderLog.created_at.desc()).limit(100).all()

        result = [
            {
                "ticker": o.ticker,
                "side": o.side,
                "quantity": float(o.quantity),
                "price": float(o.price_snapshot) if o.price_snapshot else None,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "status": o.status or "unknown",
            }
            for o in orders
        ]

        return jsonify(result)
    except Exception as e:
        logger.error(f"[obligacje] /historia error: {e}")
        return jsonify({"error": str(e)}), 500


@obligacje_bp.route("/", methods=["GET"])
@login_required
def view():
    """GET /obligacje/ - Strona dashboarda obligacji."""
    return render_template("obligacje.html")
