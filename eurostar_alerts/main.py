from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .control import ControlStore, effective_configs
from .models import CheckOutcome, CheckStatus, Provider, ScrapeReport
from .notifier import restore_failed_alerts, send_whatsapp_hits, send_check_report
from .scrapers import run_with_browser
from .state import AlertState
from .status_report import make_check_report


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
    request_id = None
    hit_searches = {}
    started_at = datetime.now(timezone.utc).isoformat()
    completion_snapshot = None
    if Path(args.controls).exists() and not args.local_config:
        control = ControlStore(config, args.controls)
        if not args.dry_run:
            request_id = control.claim_request()
        controls = control.read()
        searches = effective_configs(config, controls, allow_global_pause=bool(request_id))
        outcomes = []
        runs = []
        for search, search_config in searches:
            if request_id:
                should_continue = lambda search=search: control.still_active(search, request_id)
            else:
                should_continue = lambda search=search: control.still_active(search)
            try:
                search_report = run_with_browser(search_config, should_continue=should_continue)
            except Exception as exc:
                # Browser startup and teardown can fail outside per-date guards.
                # Record an explicit failure so a report never says zero matches.
                print(f"Scanner failed before date checks: {type(exc).__name__}")
                route = search_config.routes[0]
                search_report = ScrapeReport([
                    CheckOutcome(provider, route.name, route.start_date, CheckStatus.FAILED,
                                 f"Scanner failed before date checks: {type(exc).__name__}")
                    for provider, enabled in (
                        (Provider.SNAP, search_config.checks["snap"]),
                        (Provider.NORMAL, search_config.checks["normal_eurostar"]),
                    ) if enabled
                ])
            runs.append((search, search_report))
            outcomes.extend(search_report.outcomes)
            for hit in search_report.hits:
                hit_searches.setdefault(hit.dedupe_key, []).append(search)
        report = ScrapeReport(outcomes)
        if not searches:
            print("Scanning is paused or all searches are paused/expired; no browser started.")
        # A skipped automatic run must retain the previous actual results. A
        # one-off request with no eligible searches gets an explicit paused report.
        if not args.dry_run and (searches or request_id):
            snapshot = make_check_report(
                controls, runs, request_id=request_id, started_at=started_at
            )
            published_requested = control.publish_report(snapshot, request_id)
            if request_id and published_requested:
                completion_snapshot = snapshot
    else:
        report = run_with_browser(config)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    if completion_snapshot is not None:
        send_check_report(config.notification, completion_snapshot)

    hits = list({hit.dedupe_key: hit for hit in report.hits}.values())
    if request_id:
        print("One-off check completed; alert dedupe state left unchanged.")
        return 1 if report.failures else 0

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
