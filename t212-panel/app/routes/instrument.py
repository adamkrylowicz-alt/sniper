"""
app/routes/instrument.py
==========================
Strona szczegolow pojedynczego instrumentu - wykres, Kup/Sprzedaj, "Twoja
inwestycja". Wspolny cel klikniecia w logo/nazwe aktywa z KAZDEGO miejsca w
appce (Warp, Focus, Watchlist, Virtual Pie, Bot) - jeden widok zamiast
duplikowania go per-tryb.

Kupno/sprzedaz i cena leca przez juz istniejace, generyczne endpointy Warp
Mode (/warp/order, /warp/quote, /warp/candles, /warp/account) - ta strona
nie dodaje wlasnej logiki tradingowej, tylko nowy uklad wokol jednego tickera.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

from ..models import Instrument
from ..services import logo_cache
from ..utils import avatar_hue, friendly_name, login_required

instrument_bp = Blueprint("instrument", __name__, url_prefix="/instrument")


@instrument_bp.route("/<ticker>", methods=["GET"])
@login_required
def detail(ticker):
    instrument = Instrument.query.get(ticker)

    # Dociagniecie logo jesli brakuje - ten sam ograniczony wzorzec co
    # warp_view()/watchlist_view(), tylko dla pojedynczego tickera.
    logo_cache.ensure_logos_auto(
        current_app.static_folder,
        current_app.config.get("LOGO_DEV_API_KEY"),
        [ticker],
    )

    name = friendly_name(instrument.name) if instrument else ""
    display_ticker = ticker.split("_")[0]

    return render_template(
        "instrument_detail.html",
        ticker=ticker,
        display_ticker=display_ticker,
        name=name,
        currency=instrument.currency_code if instrument else "",
        is_leveraged=bool(instrument.is_leveraged) if instrument else False,
        hue=avatar_hue(ticker),
        initial=(name or display_ticker)[0].upper(),
        logo_filename=logo_cache.get_cached_logo_filename(current_app.static_folder, ticker),
    )
