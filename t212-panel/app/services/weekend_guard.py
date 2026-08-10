"""
app/services/weekend_guard.py
================================
Weekendowe zawieszenie stop-lossów (Adam, 2026-08-10): "premarket USA otwiera
się w niedzielę w nocy, boję się że luka na otwarciu wytnie mi SL po
najgorszej cenie na chwilowym, szybkim squeeze'u w dół, który i tak zaraz
odbije - gram tylko longi, wolę to przespać i zobaczyć skutki rano niż
zrealizować stratę na czymś co się odwróci". Backtestem (2026-08-10,
run_gap_stop_backtest w tej sesji) potwierdzone: "przeczekaj dużą lukę
zamiast realizować SL po gorszej cenie" wygrywa konsekwentnie na total
return, drawdown I liczbie stratnych pozycji - na obu testowanych progach
(1%/2%) i na out-of-sample. Tu: PRAWDZIWE anulowanie/przywracanie
zlecenia STOP na T212 (nie tylko pauza logiki bota - resting order i tak
wykonałby się na luce niezależnie od tego co robi appka), więc pozycja
jest NAGA (zero ochrony) w oknie piątek 21:00 - poniedziałek 11:00
(Europe/Amsterdam) - świadomy wybór Adama, nie domyślne zachowanie dla
nikogo innego.

Harmonogram (app/__init__.py::_register_scheduler): suspend_all piątek
21:00, restore_all poniedziałek 11:00. Manualny override: /slpause i
/slresume na Telegramie (telegram_commands.py) - na wypadek gdyby Adam
chciał zdjąć/przywrócić wcześniej niż harmonogram.

Dotyczy WSZYSTKICH 3 silników naraz (Micro-Grid/Sygnał/EOD) - Adam
świadomie wybrał to zamiast ograniczenia do jednego silnika.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytz

from ..extensions import db
from ..models import ActiveTrade, BotAuditLog, EODAuditLog, EODTrade, RiskSettings, SignalAuditLog, SignalTrade
from ..routes.api_keys import get_decrypted_credentials
from ..utils import current_environment, telegram_env_tag, ticker_display_name
from . import bot_credentials, telegram_notify
from .t212_client import T212APIError, T212Client

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

# (nazwa, model_trade, model_audit_log)
_ENGINES = (
    ("Micro-Grid", ActiveTrade, BotAuditLog),
    ("Sygnał", SignalTrade, SignalAuditLog),
    ("EOD", EODTrade, EODAuditLog),
)


def in_suspension_window(now: dt.datetime | None = None) -> bool:
    """Piątek 21:00 - poniedziałek 11:00 czasu Amsterdamu."""
    local = now or dt.datetime.now(_AMSTERDAM_TZ)
    if local.tzinfo is None:
        local = _AMSTERDAM_TZ.localize(local)
    else:
        local = local.astimezone(_AMSTERDAM_TZ)
    weekday = local.weekday()  # poniedziałek=0 ... piątek=4, sobota=5, niedziela=6
    if weekday == 4 and local.hour >= 21:
        return True
    if weekday in (5, 6):
        return True
    if weekday == 0 and local.hour < 11:
        return True
    return False


def _get_client(user_id: int, env: str) -> T212Client | None:
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return None
    creds = get_decrypted_credentials(user_id, master_key, env)
    if creds is None:
        return None
    return T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"],
        environment=env, engine="weekend_guard", user_id=user_id,
    )


def suspend_all(app) -> None:
    """Anuluje żywe zlecenia STOP dla wszystkich otwartych, uzbrojonych
    pozycji (3 silniki) i oznacza je jako zawieszone - patrz docstring
    modułu. Cena, na której stał stop, zostaje w bazie (stop_target_price/
    stop_loss_price) do ponownego wystawienia przez restore_all."""
    with app.app_context():
        token = app.config.get("TELEGRAM_BOT_TOKEN")
        chat_id = app.config.get("TELEGRAM_CHAT_ID")

        for risk in RiskSettings.query.all():
            user_id = risk.user_id
            env = current_environment(user_id)
            client = _get_client(user_id, env)
            if client is None:
                continue

            suspended: list[tuple[str, str]] = []
            for name, model, audit_model in _ENGINES:
                trades = model.query.filter_by(
                    user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True,
                    sl_suspended_for_weekend=False, environment=env,
                ).all()
                for trade in trades:
                    if not trade.stop_order_id:
                        continue  # jeszcze nieuzbrojony - nic do zawieszenia
                    try:
                        client.cancel_order(trade.stop_order_id)
                    except T212APIError as exc:
                        db.session.add(audit_model(
                            user_id=user_id, action_type="ERROR", environment=env,
                            message=f"{trade.ticker}: nie udało się anulować SL przed weekendem - {exc}. Zostaje aktywny.",
                        ))
                        continue
                    trade.stop_order_id = None
                    trade.sl_suspended_for_weekend = True
                    suspended.append((name, trade.ticker))
                    db.session.add(audit_model(
                        user_id=user_id, action_type="INFO", environment=env,
                        message=f"{trade.ticker}: SL zawieszony na weekend (pt 21:00 - pon 11:00), pozycja bez ochrony do przywrócenia.",
                    ))
            db.session.commit()

            if suspended and token and chat_id:
                lines = "\n".join(f"  {icon} {ticker_display_name(t)} ({n})" for n, t, icon in
                                   ((n, t, "🔷" if n == "Micro-Grid" else "⚡" if n == "Sygnał" else "🌙") for n, t in suspended))
                telegram_notify.send_telegram_message(
                    token, chat_id,
                    f"🌙 [{telegram_env_tag(user_id)}] SL zawieszony na weekend dla {len(suspended)} pozycji "
                    f"(BEZ OCHRONY do poniedziałku 11:00 albo /slresume):\n{lines}",
                )


def restore_all(app) -> None:
    """Przywraca zlecenia STOP dla wszystkich pozycji zawieszonych przez
    suspend_all - na cenie zapamiętanej w stop_target_price/stop_loss_price
    (bez ponownego liczenia trailingu - normalny tick to zrobi na kolejnym
    cyklu, tu tylko przywracamy JAKĄŚ ochronę jak najszybciej)."""
    with app.app_context():
        token = app.config.get("TELEGRAM_BOT_TOKEN")
        chat_id = app.config.get("TELEGRAM_CHAT_ID")

        for risk in RiskSettings.query.all():
            user_id = risk.user_id
            env = current_environment(user_id)
            client = _get_client(user_id, env)
            if client is None:
                continue

            restored: list[tuple[str, str]] = []
            failed: list[tuple[str, str, str]] = []
            for name, model, audit_model in _ENGINES:
                trades = model.query.filter_by(
                    user_id=user_id, status="OPEN", sl_suspended_for_weekend=True, environment=env,
                ).all()
                for trade in trades:
                    stop_price = getattr(trade, "stop_target_price", None)
                    if stop_price is None:
                        stop_price = getattr(trade, "stop_loss_price", None)
                    if stop_price is None:
                        trade.sl_suspended_for_weekend = False
                        failed.append((name, trade.ticker, "brak zapamiętanej ceny stopu"))
                        continue
                    try:
                        result = client.place_stop_order(trade.ticker, -trade.quantity, stop_price)
                    except T212APIError as exc:
                        real_qty = None
                        try:
                            position = client.get_position(trade.ticker)
                            if position is not None:
                                real_qty = Decimal(str(position.get("quantity")))
                        except T212APIError:
                            pass
                        if real_qty is not None and 0 < real_qty < trade.quantity:
                            try:
                                result = client.place_stop_order(trade.ticker, -real_qty, stop_price)
                            except T212APIError as exc2:
                                exc = exc2
                            else:
                                trade.stop_order_id = result.order_id
                                trade.sl_suspended_for_weekend = False
                                restored.append((name, trade.ticker))
                                db.session.add(audit_model(
                                    user_id=user_id, action_type="INFO", environment=env,
                                    message=f"{trade.ticker}: SL przywrócony po weekendzie na {stop_price:.4f} "
                                    f"(ilość skorygowana do prawdziwej z T212: {real_qty}, w bazie było {trade.quantity}).",
                                ))
                                continue
                        trade.sl_suspended_for_weekend = False
                        failed.append((name, trade.ticker, str(exc)))
                        db.session.add(audit_model(
                            user_id=user_id, action_type="ERROR", environment=env,
                            message=f"{trade.ticker}: nie udało się przywrócić SL po weekendzie - {exc}. "
                            "Odblokowana do zwykłego ticku (rekoncyliacja/ponowne uzbrojenie w ciągu ~1 min), "
                            "sprawdź na wszelki wypadek ręcznie.",
                        ))
                        continue
                    trade.stop_order_id = result.order_id
                    trade.sl_suspended_for_weekend = False
                    restored.append((name, trade.ticker))
                    db.session.add(audit_model(
                        user_id=user_id, action_type="INFO", environment=env,
                        message=f"{trade.ticker}: SL przywrócony po weekendzie na {stop_price:.4f}.",
                    ))
            db.session.commit()

            if (restored or failed) and token and chat_id:
                lines = [f"✅ {ticker_display_name(t)} ({n})" for n, t in restored]
                lines += [f"⚠️ {ticker_display_name(t)} ({n}) - {err}" for n, t, err in failed]
                telegram_notify.send_telegram_message(
                    token, chat_id,
                    f"☀️ [{telegram_env_tag(user_id)}] SL po weekendzie:\n" + "\n".join(lines),
                )
