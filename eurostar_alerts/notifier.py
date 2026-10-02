from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
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



class CheckReportRejected(RuntimeError):
    """Twilio explicitly rejected creation; retry cannot duplicate that attempt."""


def prepare_check_report(config: NotificationConfig, report: dict) -> dict:
    from .status_report import format_check_report

    _, from_number, to_number = twilio_client(config)
    # Keep the full scan timestamp, including its subsecond precision, in the
    # exact body used for request-specific reconciliation. No internal SID is shown.
    body = format_check_report(report)
    first_line = "Eurostar check finished " + report["completed_at"].replace("T", " ")
    body = first_line + "\n" + body.split("\n", 1)[1]
    return {"request_id": report["request_id"], "body": body,
            "from": from_number, "to": to_number,
            "started_at": datetime.now(timezone.utc).isoformat()}


def send_check_report(config: NotificationConfig, attempt: dict):
    """Send only a previously persisted exact attempt."""
    from twilio.base.exceptions import TwilioRestException

    client, from_number, to_number = twilio_client(config)
    if attempt["from"] != from_number or attempt["to"] != to_number:
        raise RuntimeError("Completion recipients changed; delivery requires reconciliation")
    try:
        return client.messages.create(body=attempt["body"], from_=from_number, to=to_number)
    except TwilioRestException as exc:
        if exc.status in {400, 401, 403, 404, 405, 413, 422, 429}:
            raise CheckReportRejected("Twilio rejected completion creation") from None
        raise


def reconcile_check_report(config: NotificationConfig, attempt: dict):
    """Find an accepted send, including queued messages without date_sent.

    An empty or truncated history is not proof of non-creation. The caller must
    retain the uncertain attempt rather than send it again.
    """
    client, _, _ = twilio_client(config)
    started = datetime.fromisoformat(attempt["started_at"])
    if started.tzinfo is None:
        raise RuntimeError("Completion attempt has no timezone; cannot reconcile safely")
    matches = []
    for message in client.messages.list(from_=attempt["from"], to=attempt["to"], limit=200):
        created = message.date_created
        if (message.sid not in attempt.get("failed_sids", [])
                and message.body == attempt["body"] and message.from_ == attempt["from"]
                and message.to == attempt["to"] and created is not None
                and created.tzinfo is not None
                and started - timedelta(seconds=5) <= created <= started + timedelta(minutes=5)):
            matches.append(message)
    if len(matches) > 1:
        raise RuntimeError("Multiple completion messages matched; delivery needs manual review")
    return matches[0] if matches else None


def check_report_delivery_status(config: NotificationConfig, sid: str) -> str:
    client, _, _ = twilio_client(config)
    return client.messages(sid).fetch().status
