"""
app/services/bond_alerts.py
===========================
Alerty Telegram dla obligacji:
- Rebalancing needed (drift > 1%)
- Coupon rate below target (4.5% annual)
- Daily loss exceeds -2%

Dedupe pattern: in-process sets (_alerted_*), resetuje się przy restarcie
(wystarczy do pracy dev/prod na tym samym koncie).
"""

from datetime import date
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

from flask import current_app
from . import telegram_notify
from ..utils import should_notify_environment, telegram_env_tag


def _send(user_id: int, text: str) -> None:
    """Wspólny wzorzec wysyłki - token/chat_id z config appki, filtr środowiska
    demo/live (patrz utils.py::should_notify_environment), tag [DEV]/[PROD]
    w treści - ten sam wzorzec co position_alerts.py."""
    if not should_notify_environment(user_id):
        return
    telegram_notify.send_telegram_message(
        current_app.config.get("TELEGRAM_BOT_TOKEN"),
        current_app.config.get("TELEGRAM_CHAT_ID"),
        f"[{telegram_env_tag(user_id)}] {text}",
    )


# In-process dedupe sets - resetują się przy restarcie procesu
_alerted_rebalance = set()      # user_id
_alerted_coupon = set()         # user_id
_alerted_daily_loss = set()     # (user_id, date)


def check_rebalancing_needed(
    user_id: int,
    bonds_pct: Decimal,
    target_pct: Decimal = Decimal("20"),
    drift_threshold: Decimal = Decimal("1"),
) -> bool:
    """
    Wysyła alert jeśli obligacje dryftują >drift_threshold od celu.
    Dedupowany per user_id, raz na restart (ponytail).
    """
    drift = abs(bonds_pct - target_pct)

    if drift < drift_threshold:
        # Poniżej progu, reset alert
        _alerted_rebalance.discard(user_id)
        return False

    if user_id in _alerted_rebalance:
        # Już wysłaliśmy, nie wysyłaj ponownie
        return False

    # Wyślij
    try:
        _send(user_id, f"⚖️ Rebalancing: Obligacje {bonds_pct:.1f}% (cel {target_pct:.0f}%), drift {drift:.1f}%")
        _alerted_rebalance.add(user_id)
        logger.info(f"[obligacje] Rebalancing alert sent for user {user_id}")
        return True
    except Exception as e:
        logger.warning(f"[obligacje] Failed to send rebalancing alert: {e}")
        return False


def check_coupon_rate(
    user_id: int,
    coupon_ytd_pct: Decimal,
    annual_target: Decimal = Decimal("4.5"),
) -> bool:
    """
    Wysyła alert jeśli YTD yield poniżej pro-rata annual target.
    Dedupowany per user_id, raz na restart (ponytail).
    """
    from datetime import datetime
    now = datetime.utcnow()
    days_elapsed = (now.timetuple().tm_yday) if now.year == now.year else 365
    expected_ytd = (annual_target * Decimal(days_elapsed) / Decimal("365")).quantize(Decimal("0.1"))

    if coupon_ytd_pct >= expected_ytd:
        # Powyżej celu, reset alert
        _alerted_coupon.discard(user_id)
        return False

    if user_id in _alerted_coupon:
        # Już wysłaliśmy, nie wysyłaj ponownie
        return False

    # Wyślij
    try:
        _send(user_id, f"📉 Kupon YTD: {coupon_ytd_pct:.1f}% (oczekiwane ~{expected_ytd:.1f}%)")
        _alerted_coupon.add(user_id)
        logger.info(f"[obligacje] Coupon alert sent for user {user_id}")
        return True
    except Exception as e:
        logger.warning(f"[obligacje] Failed to send coupon alert: {e}")
        return False


def check_daily_loss(
    user_id: int,
    bonds_value_today: Decimal,
    bonds_value_yesterday: Decimal,
    loss_threshold_pct: Decimal = Decimal("2"),
) -> bool:
    """
    Wysyła alert jeśli dzisiejsza strata >= loss_threshold_pct.
    Dedupowany per (user_id, date) — raz na dzień (ponytail).
    """
    today = date.today()
    key = (user_id, today)

    if bonds_value_yesterday <= 0:
        # Nie mogę liczyć % bez baseline
        _alerted_daily_loss.discard(key)
        return False

    loss_pct = ((bonds_value_yesterday - bonds_value_today) / bonds_value_yesterday * Decimal("100")).quantize(Decimal("0.1"))

    if loss_pct < loss_threshold_pct:
        # Poniżej progu, reset alert
        _alerted_daily_loss.discard(key)
        return False

    if key in _alerted_daily_loss:
        # Już wysłaliśmy dzisiaj, nie wysyłaj ponownie
        return False

    # Wyślij
    try:
        _send(user_id, f"⚠️ Strata dzisiejsza: -{loss_pct:.1f}% ({bonds_value_yesterday - bonds_value_today:.2f}€)")
        _alerted_daily_loss.add(key)
        logger.info(f"[obligacje] Daily loss alert sent for user {user_id}")
        return True
    except Exception as e:
        logger.warning(f"[obligacje] Failed to send daily loss alert: {e}")
        return False
