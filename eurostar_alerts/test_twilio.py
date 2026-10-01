"""Explicit, manual Twilio diagnostics; no secret values or message bodies in logs."""
from __future__ import annotations
import argparse
import time
from collections import Counter
from datetime import datetime, timezone

from twilio.base.exceptions import TwilioRestException

from .config import load_config
from .notifier import twilio_client

GUIDANCE = {
    63015: "Rejoin your Twilio WhatsApp Sandbox using its join code; membership expires after three days.",
    63016: "Send TEST from your phone to open WhatsApp's 24-hour reply window, then rerun this test.",
    21610: "Recipient opted out. Rejoin the Sandbox or opt back in before retrying.",
    20003: "Check the Twilio account SID and auth token repository secrets.",
    63038: "The account exceeded its WhatsApp messaging limit; check the Twilio Console.",
}


def delivery_result(message):
    if message.status in {"delivered", "read"}:
        return True, f"Test message delivery confirmed ({message.status})."
    if message.status in {"failed", "undelivered"}:
        code = message.error_code
        return False, f"Test message {message.status}; Twilio error {code}. " + GUIDANCE.get(code, "Check Twilio Messaging Logs for details.")
    return False, f"Twilio status is {message.status}; delivery to the phone is not yet confirmed."


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Send one test WhatsApp message to the configured owner")
    args = parser.parse_args(argv)
    config = load_config("config.yml")
    try:
        client, sender, owner = twilio_client(config.notification)
        recent = client.messages.list(from_=sender, to=owner, limit=5)
        print("Recent outbound delivery statuses:", dict(Counter(message.status for message in recent)))
        errors = sorted({message.error_code for message in recent if message.error_code})
        if errors:
            print("Recent Twilio error codes:", errors)
            for code in errors:
                print(GUIDANCE.get(code, "Check Twilio Messaging Logs for details."))
        incoming = client.messages.list(from_=owner, to=sender, limit=1)
        if incoming:
            when = incoming[0].date_created
            print("Latest incoming message at:", when.isoformat() if when else "unknown")
            if when:
                print("Within 24-hour reply window:", (datetime.now(timezone.utc) - when).total_seconds() < 86400)
        else:
            print("No incoming message found from the configured owner.")
        if not args.send:
            return 0
        message = client.messages.create(
            from_=sender, to=owner,
            body="Eurostar bot connection test. If you received this, WhatsApp delivery works. After the webhook is connected, send TEST or HELP. Use SCAN STOP to pause scans.",
        )
        print("Test message accepted by Twilio; checking delivery.")
        deadline = time.monotonic() + 45
        while message.status not in {"delivered", "read", "failed", "undelivered"} and time.monotonic() < deadline:
            time.sleep(3)
            message = client.messages(message.sid).fetch()
        ok, text = delivery_result(message)
        print(text)
        return 0 if ok else 1
    except TwilioRestException as exc:
        print(f"Twilio request failed: HTTP {exc.status}, code {exc.code}.")
        print(GUIDANCE.get(exc.code, "Check Twilio Messaging Logs and account status."))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
