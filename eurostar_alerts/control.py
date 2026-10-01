"""Read phone-managed searches from Twilio Sync; never fall back on stale settings."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
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


def effective_configs(config: AppConfig, data: dict, today=None) -> list[tuple[dict, AppConfig]]:
    if data.get("version") != 1 or not isinstance(data.get("searches"), list):
        raise ValueError("Invalid phone control document")
    if not data["enabled"]:
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


class ControlStore:
    def __init__(self, config: AppConfig, metadata_path: str | Path):
        metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        client, _, _ = twilio_client(config.notification)
        self.document = client.sync.v1.services(metadata["sync_service_sid"]).documents("controls")
        self.config = config

    def read(self) -> dict:
        return self.document.fetch().data

    def still_active(self, original: dict) -> bool:
        # Read before each date check and each notification so STOP/edits take effect
        # during a running job. A request already in flight cannot be recalled.
        return any(search == original for search, _ in effective_configs(self.config, self.read()))
