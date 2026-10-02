from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .control import ControlStore, effective_configs
from .models import CheckOutcome, CheckStatus, Provider, ScrapeReport
from .notifier import (
    check_report_delivery_status, restore_failed_alerts, send_check_report, send_whatsapp_hits,
)
from .scrapers import run_with_browser
from .state import AlertState
from .status_report import make_check_report


def _advance_check_delivery(control: ControlStore, notification) -> None:
    pending = control.delivery_state()
    if pending is None:
        return
    request, _ = pending
    request_id = request["id"]
    report = control.revalidate_delivery_report(request_id)
    if report is None:
        return
    if request["status"] == "report_ready":
        # A definite create failure leaves report_ready intact for the next run.
        message = send_check_report(notification, report)
        if not control.update_delivery(request_id, "report_ready", "delivery_pending",
                                       pending_sid=message.sid):
            return
        status = message.status
    else:
        # Never create another message while the saved SID is still pending.
        status = check_report_delivery_status(notification, request["pending_sid"])
    if status in {"delivered", "read"}:
        control.update_delivery(request_id, "delivery_pending", "delivered")
    elif status in {"failed", "undelivered"}:
        control.update_delivery(request_id, "delivery_pending", "report_ready")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Eurostar Snap and normal fares, then send WhatsApp alerts.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file")
    parser.add_argument("--state", default="data/notified.json", help="Path to duplicate-alert state file")
    parser.add_argument("--report", default="debug/report.json", help="Path to the machine-readable health report")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without sending WhatsApp alerts")
    parser.add_argument("--snapshot-only", action="store_true",
                        help="Publish current results without WhatsApp messages or alert-state changes")
    parser.add_argument("--controls", default="data/twilio-control.json", help="Twilio control connection metadata")
    parser.add_argument("--local-config", action="store_true", help="Use YAML only (requires --dry-run)")
    args = parser.parse_args(argv)
    if args.local_config and not args.dry_run:
        parser.error("--local-config requires --dry-run")
    if args.snapshot_only and (args.dry_run or args.local_config):
        parser.error("--snapshot-only cannot be combined with --dry-run or --local-config")
    if args.snapshot_only and not Path(args.controls).exists():
        parser.error("--snapshot-only requires connected phone controls")

    config = load_config(args.config)
    control = None
    request_id = None
    hit_searches = {}
    started_at = datetime.now(timezone.utc).isoformat()
    if Path(args.controls).exists() and not args.local_config:
        control = ControlStore(config, args.controls)
        if not args.dry_run and not args.snapshot_only:
            request_id = control.claim_request()
        controls = control.read()
        searches = effective_configs(config, controls, allow_global_pause=bool(request_id) or args.snapshot_only)
        outcomes = []
        runs = []
        interrupted_ids = set()
        for search, search_config in searches:
            if request_id:
                should_continue = lambda search=search: control.still_active(search, request_id)
            elif args.snapshot_only:
                should_continue = lambda search=search: control.still_active(
                    search, allow_global_pause=True
                )
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
            if not should_continue():
                interrupted_ids.add(search["id"])
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
                controls, runs, request_id=request_id, started_at=started_at,
                interrupted_ids=interrupted_ids, manual_snapshot=args.snapshot_only,
            )
            control.publish_report(snapshot, request_id)
    else:
        report = run_with_browser(config)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    if control and not args.dry_run and not args.snapshot_only:
        _advance_check_delivery(control, config.notification)

    hits = list({hit.dedupe_key: hit for hit in report.hits}.values())
    if request_id:
        print("One-off check completed; alert dedupe state left unchanged.")
        return 1 if report.failures else 0
    if args.snapshot_only:
        print("Results snapshot published; no WhatsApp messages or alert-state changes.")
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
