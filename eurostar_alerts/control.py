"""Read phone-managed searches from Twilio Sync; never fall back on stale settings."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .models import RouteQuery
from .notifier import twilio_client


def seed_controls(config: AppConfig) -> dict:
    snap = config.checks.get("snap", True)
    normal = config.checks.get("normal_eurostar", True)
    mode = "both" if snap and normal else "snap" if snap else "normal"
    return {
        "version": 1, "enabled": True, "timezone": config.settings.timezone,
        "next_id": len(config.routes) + 1, "processed": [],
        "searches": [
            {
                "id": f"s{index}", "name": route.name, "origin": route.origin,
                "destination": route.destination, "start_date": str(route.start_date),
                "end_date": str(route.end_date), "passengers": route.passengers,
                "enabled": True, "mode": mode, "max": config.normal_eurostar.threshold_amount,
            }
            for index, route in enumerate(config.routes, 1)
        ],
    }


def effective_configs(config: AppConfig, data: dict, today=None, *, allow_global_pause=False) -> list[tuple[dict, AppConfig]]:
    if data.get("version") != 1 or not isinstance(data.get("searches"), list):
        raise ValueError("Invalid phone control document")
    if not data["enabled"] and not allow_global_pause:
        return []
    today = today or datetime.now(ZoneInfo(data["timezone"])).date()
    result = []
    for search in data["searches"]:
        route = RouteQuery(
            name=search["name"], origin=search["origin"], destination=search["destination"],
            start_date=datetime.strptime(search["start_date"], "%Y-%m-%d").date(),
            end_date=datetime.strptime(search["end_date"], "%Y-%m-%d").date(),
            passengers=int(search["passengers"]),
        )
        if not search["enabled"] or route.end_date < today:
            continue
        mode = search["mode"]
        if mode not in {"snap", "normal", "both"} or not 1 <= route.passengers <= 4:
            raise ValueError("Invalid search settings in phone controls")
        if route.end_date < route.start_date or not 0 < float(search["max"]) <= 500:
            raise ValueError("Invalid dates or price in phone controls")
        result.append((search, replace(
            config,
            settings=replace(config.settings, timezone=data["timezone"]),
            routes=[route],
            checks={"snap": mode in {"snap", "both"}, "normal_eurostar": mode in {"normal", "both"}},
            normal_eurostar=replace(config.normal_eurostar, threshold_amount=float(search["max"])),
        )))
    return result


REPORT_SETTINGS = ("origin", "destination", "start_date", "end_date", "passengers", "mode", "max", "enabled")


def _same_report_settings(current: dict, saved: dict) -> bool:
    return all(
        float(current[key]) == float(saved[key]) if key in {"max", "passengers"}
        else current[key] == saved[key]
        for key in REPORT_SETTINGS
    )


def _revalidate_report(report: dict, controls: dict) -> None:
    """Remove invalidated matches and disclose incomplete search coverage."""
    today = datetime.now(ZoneInfo(controls["timezone"])).date().isoformat()
    current = {search["id"]: search for search in controls["searches"]}
    recorded = {search["id"]: search for search in report["searches"]}
    valid_ids = set()
    notes = list(report.get("coverage_notes", []))
    for search_id, saved in recorded.items():
        present = current.get(search_id)
        current_status = (
            "expired" if present and present["end_date"] < today
            else "active" if present and present["enabled"] else "paused"
        )
        if not present or not _same_report_settings(present, saved["settings"]) or current_status != saved["status"]:
            notes.append(f"Search {search_id} changed, paused or expired during check")
        elif current_status == "active" and (
            controls["enabled"] or report["request_id"] or report.get("manual_snapshot")
        ):
            valid_ids.add(search_id)
    for search_id in current:
        if search_id not in recorded:
            notes.append(f"Search {search_id} was added after this check began")
    if not controls["enabled"] and not report["request_id"] and not report.get("manual_snapshot") and recorded:
        notes.append("Automatic monitoring was paused during this check")
        valid_ids.clear()
    if notes:
        report["coverage_changed"] = True
        report["coverage_notes"] = list(dict.fromkeys(notes))[:8]
        report["state"] = "partial"
    report["hits"] = [hit for hit in report["hits"] if hit["search_id"] in valid_ids]
    report["errors"] = [error for error in report["errors"] if error["search_id"] in valid_ids]
    report["hit_count"] = sum(recorded[search_id]["hit_count"] for search_id in valid_ids)
    report["omitted_hits"] = max(0, report["hit_count"] - len(report["hits"]))


class ControlStore:
    def __init__(self, config: AppConfig, metadata_path: str | Path):
        metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        client, _, _ = twilio_client(config.notification)
        self.document = client.sync.v1.services(metadata["sync_service_sid"]).documents("controls")
        self.config = config

    def read(self) -> dict:
        return self.document.fetch().data

    def claim_request(self) -> str | None:
        """Claim one queued phone request without overwriting concurrent edits."""
        for _ in range(3):
            current = self.document.fetch()
            request = current.data.get("check_request") or {}
            if request.get("status") == "running":
                try:
                    started = datetime.fromisoformat(request["started_at"])
                    stale = started.tzinfo is not None and datetime.now(timezone.utc) - started > timedelta(minutes=20)
                except (KeyError, TypeError, ValueError):
                    stale = True
                if not stale:
                    return None
            elif request.get("status") != "queued":
                return None
            updated = deepcopy(current.data)
            updated["check_request"] = {
                **request, "status": "running", "started_at": datetime.now(timezone.utc).isoformat()
            }
            try:
                self.document.update(data=updated, if_match=current.revision)
                return request["id"]
            except Exception as exc:
                if getattr(exc, "status", None) != 412:
                    raise
        raise RuntimeError("Could not claim check request after concurrent control updates")

    def publish_report(self, report: dict, request_id: str | None = None) -> bool:
        """Publish a bounded snapshot, completing only this run's request."""
        for _ in range(3):
            current = self.document.fetch()
            request = current.data.get("check_request") or {}
            if request_id and (request.get("id") != request_id or request.get("status") != "running"):
                return False
            if not request_id and request.get("status") in {"report_ready", "delivery_pending"}:
                return False  # Preserve the one outstanding delivery and its saved report.
            updated = deepcopy(current.data)
            updated["check_report"] = deepcopy(report)
            _revalidate_report(updated["check_report"], current.data)
            if request_id:
                updated["check_request"] = {**request, "status": "report_ready"}
            snapshot = updated["check_report"]
            while len(json.dumps(updated, ensure_ascii=False).encode("utf-8")) > 14_000:
                if snapshot["hits"]:
                    snapshot["hits"].pop()
                    snapshot["omitted_hits"] += 1
                elif snapshot["errors"]:
                    snapshot["errors"].pop()
                    snapshot["omitted_errors"] += 1
                else:
                    raise RuntimeError("Phone controls and scan report exceed Sync storage limit")
            try:
                self.document.update(data=updated, if_match=current.revision)
                report.clear()
                report.update(snapshot)
                return bool(request_id)
            except Exception as exc:
                if getattr(exc, "status", None) != 412:
                    raise
        raise RuntimeError("Could not publish scan report after concurrent control updates")

    def still_active(self, original: dict, request_id: str | None = None,
                     *, allow_global_pause: bool = False) -> bool:
        # Read before each date check and each notification so STOP/edits take effect
        # during a running job. A request already in flight cannot be recalled.
        data = self.read()
        if request_id and not (
            (data.get("check_request") or {}).get("id") == request_id
            and (data.get("check_request") or {}).get("status") == "running"
        ):
            return False
        return any(
            search == original for search, _ in effective_configs(
                self.config, data, allow_global_pause=bool(request_id) or allow_global_pause
            )
        )


    def delivery_state(self) -> tuple[dict, dict] | None:
        data = self.read()
        request = data.get("check_request") or {}
        report = data.get("check_report") or {}
        if request.get("status") not in {"report_ready", "delivery_pending"}:
            return None
        if report.get("request_id") != request.get("id"):
            raise RuntimeError("Completion report does not match pending request")
        return request, report

    def update_delivery(self, request_id: str, expected_status: str, status: str,
                        *, pending_sid: str | None = None) -> bool:
        for _ in range(3):
            current = self.document.fetch()
            request = current.data.get("check_request") or {}
            if request.get("id") != request_id or request.get("status") != expected_status:
                return False
            updated = deepcopy(current.data)
            next_request = {**request, "status": status}
            if pending_sid:
                next_request["pending_sid"] = pending_sid
            elif status != "delivery_pending":
                next_request.pop("pending_sid", None)
            updated["check_request"] = next_request
            try:
                self.document.update(data=updated, if_match=current.revision)
                return True
            except Exception as exc:
                if getattr(exc, "status", None) != 412:
                    raise
        raise RuntimeError("Could not update completion delivery after concurrent control updates")

    def revalidate_delivery_report(self, request_id: str) -> dict | None:
        """Apply current settings before each delivery attempt or retry."""
        for _ in range(3):
            current = self.document.fetch()
            request = current.data.get("check_request") or {}
            report = current.data.get("check_report") or {}
            if (request.get("id") != request_id
                    or request.get("status") not in {"report_ready", "delivery_pending"}
                    or report.get("request_id") != request_id):
                return None
            updated = deepcopy(current.data)
            _revalidate_report(updated["check_report"], current.data)
            if updated["check_report"] == report:
                return report
            try:
                self.document.update(data=updated, if_match=current.revision)
                return updated["check_report"]
            except Exception as exc:
                if getattr(exc, "status", None) != 412:
                    raise
        raise RuntimeError("Could not revalidate completion report after concurrent control updates")
