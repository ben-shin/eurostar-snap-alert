"""A bounded snapshot of the latest completed scan, independent of alert dedupe."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import ScrapeReport

MAX_HITS = 12
MAX_ERRORS = 12
SETTINGS_FIELDS = ("origin", "destination", "start_date", "end_date", "passengers", "mode", "max", "enabled")


def make_check_report(
    controls: dict,
    runs: list[tuple[dict, ScrapeReport]],
    *,
    request_id: str | None,
    started_at: str,
    interrupted_ids: set[str] | None = None,
    manual_snapshot: bool = False,
) -> dict:
    today = datetime.now(ZoneInfo(controls["timezone"])).date().isoformat()
    by_id = {search["id"]: report for search, report in runs}
    interrupted_ids = interrupted_ids or set()
    searches = []
    all_hits = []
    all_errors = []
    attempted = completed = failed = 0
    for search in controls["searches"]:
        report = by_id.get(search["id"], ScrapeReport([]))
        if search["end_date"] < today:
            status = "expired"
        elif not search["enabled"]:
            status = "paused"
        else:
            status = "active"
        searches.append({
            "id": search["id"],
            "status": status,
            "settings": {key: search[key] for key in SETTINGS_FIELDS},
            "attempted": report.attempted,
            "completed": report.completed,
            "failed": len(report.failures),
            "hit_count": len(report.hits),
        })
        attempted += report.attempted
        completed += report.completed
        failed += len(report.failures)
        for hit in report.hits:
            all_hits.append({
                "search_id": search["id"],
                "provider": hit.provider.value,
                "route": hit.route_name,
                "date": hit.travel_date.isoformat(),
                "price": hit.price_amount,
                "currency": hit.currency,
                "departure_time": hit.departure_time,
                "arrival_time": hit.arrival_time,
                "departure_window": hit.departure_window,
                "duration": hit.duration,
                "fare_class": hit.fare_class,
                "booking_url": hit.booking_url,
            })
        for outcome in report.failures:
            all_errors.append({
                "search_id": search["id"], "provider": outcome.provider.value,
                "date": outcome.travel_date.isoformat(), "message": outcome.message[:180],
            })
    # The Sync control document is limited to 16 KB. Keep counts for every
    # search even when the detailed fare/error list needs to be shortened.
    all_hits.sort(key=lambda hit: (hit["price"] is None, hit["price"] or 0, hit["date"]))
    state = "paused" if not runs else "failed" if attempted and completed == 0 and not interrupted_ids else "partial" if failed or interrupted_ids else "complete"
    return {
        "request_id": request_id,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "automatic_enabled": controls["enabled"],
        "manual_snapshot": manual_snapshot,
        "state": state,
        "coverage_changed": bool(interrupted_ids),
        "coverage_notes": [f"Search {search_id} was interrupted" for search_id in sorted(interrupted_ids)][:8],
        "attempted": attempted,
        "completed": completed,
        "failed": failed,
        "hit_count": len(all_hits),
        "omitted_hits": max(0, len(all_hits) - MAX_HITS),
        "omitted_errors": max(0, len(all_errors) - MAX_ERRORS),
        "searches": searches,
        "hits": all_hits[:MAX_HITS],
        "errors": all_errors[:MAX_ERRORS],
    }


def format_check_report(report: dict) -> str:
    """Concise WhatsApp completion, with counts even when details are capped."""
    lines = [
        "Eurostar check finished " + report["completed_at"][:16].replace("T", " ") + " UTC.",
        f"{report['completed']} of {report['attempted']} date checks completed; {report['failed']} failed.",
        f"Matches found in this check: {report['hit_count']}.",
    ]
    if report.get("coverage_changed"):
        lines.append("Search settings changed or a check was interrupted; this report is partial.")
    if report["state"] == "paused":
        lines.append("No eligible searches were checked. Automatic monitoring settings were not changed.")
    elif report["attempted"] == 0 and report.get("coverage_changed"):
        lines.append("The check was interrupted before completing date checks.")
    elif report["attempted"] == 0:
        lines.append("No travel dates were in the current booking window.")
    shown_hits = 0
    for hit in report["hits"]:
        price = "Snap fare" if hit["price"] is None else f"{hit['currency']} {hit['price']:g}"
        detail = f"{hit['departure_time']} to {hit['arrival_time']}" if hit["departure_time"] and hit["arrival_time"] else (hit["departure_window"] or "times unavailable")
        entry = f"{hit['search_id']} {hit['provider']} {hit['date']}: {price}, {detail}"
        if hit["fare_class"]:
            entry += f", {hit['fare_class']}"
        entry += f"\n{hit['booking_url']}"
        if len("\n".join(lines + [entry, "Send RESULTS for the latest report."])) > 1100:
            break
        lines.append(entry)
        shown_hits += 1
    if report["omitted_hits"] or len(report["hits"]) > shown_hits:
        lines.append("More hits are counted above; send RESULTS for the saved details.")
    if report["failed"]:
        lines.append("Some fare checks failed. Send RESULTS for the affected dates.")
    lines.append("Send RESULTS for the latest report.")
    return "\n".join(lines)
