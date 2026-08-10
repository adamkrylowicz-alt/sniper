"""
app/services/daily_summary.py
================================
Podsumowanie P&L WSZYSTKICH 3 silników na Telegram, dwa razy dziennie
(Adam, 2026-08-05: "dodaj /status i dzienne podsumowanie P&L wieczorem i
rano" - patrz app/__init__.py::_register_scheduler, dwa cron joby).

CELOWO OSOBNE od bot_engine.py::daily_report() - tamten istnieje od dawna,
wysyła mailem, i dotyczy WYŁĄCZNIE Micro-Gridu (ActiveTrade). Ten moduł
pokrywa WSZYSTKIE 3 silniki (Micro-Grid+Sygnał+EOD) + całość konta (patrz
routes/scalping.py::_account_summary, ten sam pomysł: equity całego konta
vs punkt startowy) w JEDNYM zwięzłym komunikacie na Telegram - inny kanał,
inna publiczność (szybki rzut oka na telefonie, nie szczegółowy mail).
Stary mailowy raport zostaje bez zmian, ten go NIE zastępuje.

Zrealizowany P&L: pozycje CLOSED z ostatnich 24h (ten sam wzorzec co
bot_engine.py::daily_report). Niezrealizowany: WSZYSTKIE aktualnie otwarte,
wycenione żywą ceną (price_feed, ta sama funkcja co decyzje botów).
Equity całego konta: JEDNO zapytanie T212 (/equity/account/summary) - bezpieczne
przy wywołaniu 2x/dzień, nie konkuruje z ciasnym limitem tick'ów co 60s.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytz
from flask import current_app

from ..extensions import db
from ..models import ActiveTrade, EODSettings, EODTrade, RiskSettings, SignalSettings, SignalTrade, User, UserSettings
from ..routes.api_keys import get_decrypted_credentials
from ..utils import current_environment, telegram_env_tag
from . import bot_credentials, price_feed, telegram_notify
from .market_data_keys import get_decrypted_market_data_keys
from .t212_client import T212APIError, T212Client

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

# Archiwum wysłanych raportów dziennych (dodane 2026-08-07, Adam: "niech się
# te raporty z telegrama gdzieś zapisują w archiwum") - do tej pory treść
# raportu istniała TYLKO jako wysłana wiadomość Telegram, nigdzie lokalnie -
# nie dało się później sprawdzić "co dokładnie wysłał raport z X", trzeba
# było przeliczać od nowa z bazy (i to tylko dla danych które NIE wygasają,
# jak zrealizowane 24h - już następnego dnia bezpowrotnie tracone). Plik
# tekstowy, append-only, jeden wpis na wysłany raport - świadomie NIE baza
# (to log do przeglądania, nie coś co appka odpytuje).
DAILY_REPORTS_ARCHIVE_FILENAME = "daily_reports_archive.log"


def _archive_report(text: str) -> None:
    path = Path(current_app.instance_path) / DAILY_REPORTS_ARCHIVE_FILENAME
    timestamp = dt.datetime.now(_AMSTERDAM_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{'=' * 60}\n{timestamp}\n{'=' * 60}\n{text}\n\n")


def _engine_pnl_24h(user_id: int, trade_model, is_paper_field: str = "is_paper", days: int = 1) -> dict:
    """
    Wspólna logika dla 3 silników - `trade_model` to ActiveTrade/SignalTrade/
    EODTrade, wszystkie mają identyczny kształt pól (buy_price/close_price/
    quantity/average_price dla Micro-Gridu, buy_price dla reszty - patrz
    niżej rozróżnienie kosztu bazowego).

    `days` (Adam, 2026-08-09: raport tygodniowy/miesięczny + `/status 7d`) -
    okno dla zrealizowanego P&L (`closed_at >= cutoff`); niezrealizowane
    zawsze dotyczy WSZYSTKICH aktualnie otwartych pozycji, niezależnie od
    okna (to "teraz", nie coś co się mieści w oknie).
    """
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
    env = current_environment(user_id)
    closed = (
        trade_model.query
        .filter_by(user_id=user_id, status="CLOSED", environment=env, **{is_paper_field: False})
        .filter(trade_model.closed_at >= cutoff)
        .all()
    )
    realized = Decimal("0")
    realized_unknown = 0
    for t in closed:
        if t.close_price is None:
            realized_unknown += 1
            continue
        # Micro-Grid liczy od average_price (koszt bazowy po DCA), Sygnał/EOD
        # od buy_price (jedno wejście, bez DCA) - oba modele mają buy_price,
        # tylko ActiveTrade ma DODATKOWO average_price jako właściwy koszt.
        cost_basis = getattr(t, "average_price", None) or t.buy_price
        realized += (t.close_price - cost_basis) * t.quantity

    open_trades = trade_model.query.filter_by(user_id=user_id, status="OPEN", environment=env, **{is_paper_field: False}).all()
    unrealized = Decimal("0")
    unrealized_unpriced = 0
    open_positions = []  # [(ticker, pnl, currency)] - patrz telegram_commands.py::_status_message
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    for t in open_trades:
        price = price_feed.get_live_price(
            market_keys.get("finnhub_api_key"), t.ticker,
            market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
        )
        if price is None or price <= 0:
            unrealized_unpriced += 1
            open_positions.append((t.ticker, None, t.currency))
            continue
        cost_basis = getattr(t, "average_price", None) or t.buy_price
        pnl = (price - cost_basis) * t.quantity
        unrealized += pnl
        open_positions.append((t.ticker, pnl, t.currency))

    return {
        "realized": realized, "realized_n": len(closed), "realized_unknown": realized_unknown,
        "unrealized": unrealized, "open_n": len(open_trades), "unrealized_unpriced": unrealized_unpriced,
        "open_positions": open_positions,
    }


def _account_total(user_id: int) -> Decimal | None:
    """Jedno zapytanie T212 (equity całego konta) - patrz docstring modułu. None gdy brak poświadczeń/błąd."""
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return None
    env = current_environment(user_id)
    creds = get_decrypted_credentials(user_id, master_key, env)
    if creds is None:
        return None
    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=env, engine="daily_summary", user_id=user_id)
    try:
        raw = client.get_cash()
        return Decimal(str(raw["total"]))
    except (T212APIError, InvalidOperation, TypeError, KeyError):
        return None


def send_daily_summary(app, label: str, days: int = 1) -> None:
    """
    `label`: "poranny", "wieczorny", "tygodniowy" albo "miesięczny" - tylko
    do treści komunikatu. `days`: okno zrealizowanego P&L (patrz
    `_engine_pnl_24h`) - 1 dla porannego/wieczornego (bez zmian), 7/30 dla
    nowych cron jobów tygodniowego/miesięcznego (Adam, 2026-08-09).
    Wołane z app/__init__.py::_register_scheduler (cztery cron joby).
    """
    with app.app_context():
        token = app.config.get("TELEGRAM_BOT_TOKEN")
        chat_id = app.config.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return  # Telegram nieskonfigurowany - nic do wysłania

        for settings in RiskSettings.query.all():
            user = User.query.get(settings.user_id)
            if user is None:
                continue

            micro = _engine_pnl_24h(user.id, ActiveTrade, days=days)
            signal_settings = SignalSettings.query.filter_by(user_id=user.id).first()
            eod_settings = EODSettings.query.filter_by(user_id=user.id).first()
            signal = _engine_pnl_24h(user.id, SignalTrade, days=days) if signal_settings else None
            eod = _engine_pnl_24h(user.id, EODTrade, days=days) if eod_settings else None

            account_total = _account_total(user.id)
            user_settings = UserSettings.query.filter_by(user_id=user.id).first()
            baseline = user_settings.account_baseline_equity if user_settings else None

            total_realized = micro["realized"] + (signal["realized"] if signal else Decimal("0")) + (eod["realized"] if eod else Decimal("0"))
            total_unrealized = micro["unrealized"] + (signal["unrealized"] if signal else Decimal("0")) + (eod["unrealized"] if eod else Decimal("0"))

            now_local = dt.datetime.now(_AMSTERDAM_TZ).strftime("%d.%m %H:%M")
            lines = [f"📊 [{telegram_env_tag(user.id)}] SNAJPER — podsumowanie {label} ({now_local})", ""]

            if account_total is not None:
                account_line = f"Całość konta: {account_total:.2f}€"
                if baseline:
                    pnl = account_total - baseline
                    pct = (pnl / baseline * 100) if baseline > 0 else Decimal("0")
                    account_line += f" (vs start {baseline:.0f}€: {pnl:+.2f}€, {pct:+.1f}%)"
                lines.append(account_line)
                lines.append("")

            window_label = "24h" if days == 1 else f"{days}d"

            def _engine_line(name: str, data: dict | None) -> str:
                if data is None:
                    return f"{name}: nieaktywny"
                extra = f", {data['realized_unknown']} bez znanej ceny" if data["realized_unknown"] else ""
                return (
                    f"{name}: zrealizowane {window_label} {data['realized']:+.2f}€ "
                    f"({data['realized_n']} zamkniętych{extra}), "
                    f"niezrealizowane {data['unrealized']:+.2f}€ ({data['open_n']} otwartych)"
                )

            lines.append(_engine_line("Micro-Grid", micro))
            lines.append(_engine_line("Sygnał", signal))
            lines.append(_engine_line("EOD", eod))
            lines.append("")
            lines.append(f"RAZEM {window_label}: {total_realized + total_unrealized:+.2f}€ (zrealizowane {total_realized:+.2f}€ + niezrealizowane {total_unrealized:+.2f}€)")

            report_text = "\n".join(lines)
            _archive_report(report_text)
            telegram_notify.send_telegram_message(token, chat_id, report_text)
