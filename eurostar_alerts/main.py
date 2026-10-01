from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .control import ControlStore, effective_configs
from .models import ScrapeReport
from .notifier import restore_failed_alerts, send_whatsapp_hits
from .scrapers import run_with_browser
from .state import AlertState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Eurostar Snap and normal fares, then send WhatsApp alerts.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file")
    parser.add_argument("--state", default="data/notified.json", help="Path to duplicate-alert state file")
    parser.add_argument("--report", default="debug/report.json", help="Path to the machine-readable health report")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without sending WhatsApp alerts")
    parser.add_argument("--controls", default="data/twilio-control.json", help="Twilio control connection metadata")
    parser.add_argument("--local-config", action="store_true", help="Use YAML only (requires --dry-run)")
    args = parser.parse_args(argv)
    if args.local_config and not args.dry_run:
        parser.error("--local-config requires --dry-run")

    config = load_config(args.config)
    control = None
    hit_searches = {}
    if Path(args.controls).exists() and not args.local_config:
        control = ControlStore(config, args.controls)
        searches = effective_configs(config, control.read())
        outcomes = []
        for search, search_config in searches:
            report = run_with_browser(search_config, should_continue=lambda: control.still_active(search))
            outcomes.extend(report.outcomes)
            for hit in report.hits:
                hit_searches.setdefault(hit.dedupe_key, []).append(search)
        report = ScrapeReport(outcomes)
        if not searches:
            print("Scanning is paused or all searches are paused/expired; no browser started.")
    else:
        report = run_with_browser(config)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")

    hits = list({hit.dedupe_key: hit for hit in report.hits}.values())
    state = AlertState(args.state)
    if not args.dry_run:
        restore_failed_alerts(config.notification, hits, state)
    unseen = state.unseen(hits)

    print(f"Total hits: {len(hits)} | New hits: {len(unseen)}")
    for hit in unseen:
        price = "any Snap fare" if hit.price_amount is None else f"{hit.currency} {hit.price_amount:g}"
        print(f"NEW: {hit.provider.value} | {hit.route_name} | {hit.travel_date} | {price} | {hit.booking_url}")

    if unseen and not args.dry_run:
        if config.notification.provider != "twilio_whatsapp":
            raise ValueError(f"Unsupported notification provider: {config.notification.provider}")
        for hit in unseen:
            if control and not any(control.still_active(search) for search in hit_searches[hit.dedupe_key]):
                print("Suppressed fare alert: search paused, expired or modified.")
                continue
            send_whatsapp_hits(config.notification, [hit], state)
    elif unseen:
        print("Dry run enabled; not sending messages or updating state.")
    elif not args.dry_run:
        # Ensure the state file exists so the workflow commit step is deterministic.
        state.save()

    print(
        "Health: "
        f"attempted={report.attempted} completed={report.completed} "
        f"failed={len(report.failures)}"
    )
    for failure in report.failures:
        print(
            "FAILED: "
            f"{failure.provider.value} | {failure.route_name} | "
            f"{failure.travel_date} | {failure.message}"
        )

    if report.failures:
        print(f"Scraper health check failed; see {report_path} and uploaded debug artifacts.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
