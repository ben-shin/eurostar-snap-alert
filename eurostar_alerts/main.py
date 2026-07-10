from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .notifier import send_whatsapp_hits
from .scrapers import run_with_browser
from .state import AlertState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Eurostar Snap and normal fares, then send WhatsApp alerts.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file")
    parser.add_argument("--state", default="data/notified.json", help="Path to duplicate-alert state file")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without sending WhatsApp alerts")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    hits = run_with_browser(config)
    state = AlertState(args.state)
    unseen = state.unseen(hits)

    print(f"Total hits: {len(hits)} | New hits: {len(unseen)}")
    for hit in unseen:
        price = "any Snap fare" if hit.price_amount is None else f"{hit.currency} {hit.price_amount:g}"
        print(f"NEW: {hit.provider.value} | {hit.route_name} | {hit.travel_date} | {price} | {hit.booking_url}")

    if unseen and not args.dry_run:
        if config.notification.provider != "twilio_whatsapp":
            raise ValueError(f"Unsupported notification provider: {config.notification.provider}")
        send_whatsapp_hits(config.notification, unseen)
        state.mark_seen(unseen)
    elif unseen:
        print("Dry run enabled; not sending messages or updating state.")
    else:
        # Ensure the state file exists so the workflow commit step is deterministic.
        Path(args.state).parent.mkdir(parents=True, exist_ok=True)
        state.save()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
