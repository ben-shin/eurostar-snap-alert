from __future__ import annotations

import os
import time
from typing import Iterable

from .config import NotificationConfig
from .models import FareHit, Provider
from .state import AlertState


def format_hit(hit: FareHit, include_details: bool = True) -> str:
    provider = "Eurostar Snap" if hit.provider == Provider.SNAP else "Normal Eurostar"
    if hit.price_amount is None:
        price = "Snap fare available"
    else:
        symbol = "£" if hit.currency == "GBP" else "€" if hit.currency == "EUR" else f"{hit.currency or ''} "
        price = f"{symbol}{hit.price_amount:g}"

    lines = [
        f"🚄 {provider} alert",
        hit.route_name,
        f"{hit.origin} → {hit.destination}",
        f"Date: {hit.travel_date.isoformat()}",
        f"Passengers: {hit.passengers}",
        f"Price: {price}",
    ]
    if include_details:
        if hit.provider == Provider.SNAP:
            if hit.departure_window:
                lines.append(f"Departure window: {hit.departure_window} ({hit.origin} local time)")
            lines.append("Departure/arrival: assigned by Eurostar; not available at booking")
        else:
            if hit.fare_class:
                lines.append(f"Class: {hit.fare_class}")
            if hit.departure_time and hit.arrival_time:
                lines.append(f"Departure: {hit.departure_time} ({hit.origin} local time)")
                lines.append(f"Arrival: {hit.arrival_time} ({hit.destination} local time)")
                if hit.duration:
                    lines.append(f"Duration: {hit.duration}")
            else:
                lines.append("Departure/arrival: not provided in this result")
    lines.extend([hit.summary, f"Book/check: {hit.booking_url}"])
    return "\n".join(lines)


def twilio_client(config: NotificationConfig):
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

    return Client(sid, token), from_number, to_number


def restore_failed_alerts(config: NotificationConfig, hits: Iterable[FareHit], state: AlertState) -> None:
    """Repair old dedupe entries only when Twilio proves the matching alert failed."""
    hits = list(hits)
    unseen_keys = {hit.dedupe_key for hit in state.unseen(hits)}
    candidates = {
        body: hit
        for hit in hits if hit.dedupe_key not in unseen_keys
        for body in (format_hit(hit), format_hit(hit, include_details=False))
    }
    if not candidates:
        return
    client, from_number, to_number = twilio_client(config)
    latest = {}
    delivered_keys = set()
    for message in client.messages.list(from_=from_number, to=to_number, limit=500):
        if message.body not in candidates or message.date_created is None:
            continue
        if message.status in {"delivered", "read"}:
            delivered_keys.add(candidates[message.body].dedupe_key)
        previous = latest.get(message.body)
        if previous is None or message.date_created > previous.date_created:
            latest[message.body] = message
    failed = list({
        candidates[body].dedupe_key: candidates[body]
        for body, message in latest.items()
        if message.status in {"failed", "undelivered"}
        and candidates[body].dedupe_key not in delivered_keys
    }.values())
    count = state.restore_failed(failed)
    if count:
        print(f"Restored {count} alerts whose previous delivery Twilio confirmed had failed.")


def send_whatsapp_hits(config: NotificationConfig, hits: Iterable[FareHit], state: AlertState) -> None:
    client, from_number, to_number = twilio_client(config)
    for hit in hits:
        pending = state.pending_sid(hit)
        if pending:
            message = client.messages(pending).fetch()
        else:
            message = client.messages.create(body=format_hit(hit), from_=from_number, to=to_number)
            state.record_pending(hit, message.sid)
        deadline = time.monotonic() + 20
        while message.status not in {"delivered", "read", "failed", "undelivered"} and time.monotonic() < deadline:
            time.sleep(3)
            message = client.messages(message.sid).fetch()
        if message.status in {"delivered", "read"}:
            state.mark_seen([hit])
        elif message.status in {"failed", "undelivered"}:
            state.clear_pending(hit)
            guidance = {
                63015: "Rejoin the Twilio WhatsApp Sandbox.",
                63016: "Send TEST from your phone to open the 24-hour WhatsApp reply window.",
                21610: "The recipient opted out; rejoin or opt back in.",
            }.get(message.error_code, "Check Twilio Messaging Logs.")
            raise RuntimeError(f"WhatsApp delivery {message.status} (code {message.error_code}). {guidance}")
        else:
            # Keep the SID: a later scan checks this message instead of sending a duplicate.
            print(f"WhatsApp delivery still {message.status}; saved pending message for next run.")



def send_check_report(config: NotificationConfig, report: dict) -> None:
    """Send a completion report for an explicitly requested fresh scan."""
    from .status_report import format_check_report

    client, from_number, to_number = twilio_client(config)
    client.messages.create(body=format_check_report(report), from_=from_number, to=to_number)
