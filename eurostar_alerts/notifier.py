from __future__ import annotations

import os
from typing import Iterable

from .config import NotificationConfig
from .models import FareHit, Provider


def format_hit(hit: FareHit) -> str:
    provider = "Eurostar Snap" if hit.provider == Provider.SNAP else "Normal Eurostar"
    if hit.price_amount is None:
        price = "Snap fare available"
    else:
        symbol = "£" if hit.currency == "GBP" else "€" if hit.currency == "EUR" else f"{hit.currency or ''} "
        price = f"{symbol}{hit.price_amount:g}"

    return (
        f"🚄 {provider} alert\n"
        f"{hit.route_name}\n"
        f"{hit.origin} → {hit.destination}\n"
        f"Date: {hit.travel_date.isoformat()}\n"
        f"Passengers: {hit.passengers}\n"
        f"Price: {price}\n"
        f"{hit.summary}\n"
        f"Book/check: {hit.booking_url}"
    )


def send_whatsapp_hits(config: NotificationConfig, hits: Iterable[FareHit]) -> None:
    sid = os.environ.get(config.twilio_account_sid_env)
    token = os.environ.get(config.twilio_auth_token_env)
    from_number = os.environ.get(config.twilio_from_whatsapp_env)
    to_number = os.environ.get(config.twilio_to_whatsapp_env)

    missing = [
        name
        for name, value in [
            (config.twilio_account_sid_env, sid),
            (config.twilio_auth_token_env, token),
            (config.twilio_from_whatsapp_env, from_number),
            (config.twilio_to_whatsapp_env, to_number),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables for Twilio WhatsApp: {', '.join(missing)}")

    from twilio.rest import Client

    client = Client(sid, token)
    for hit in hits:
        client.messages.create(body=format_hit(hit), from_=from_number, to=to_number)
