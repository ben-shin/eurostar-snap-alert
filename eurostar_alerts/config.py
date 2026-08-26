from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .models import RouteQuery


@dataclass(frozen=True)
class Settings:
    timezone: str
    headless: bool
    debug: bool
    user_agent: str
    snap_max_days_ahead: int
    page_timeout_ms: int


@dataclass(frozen=True)
class NotificationConfig:
    provider: str
    twilio_account_sid_env: str
    twilio_auth_token_env: str
    twilio_from_whatsapp_env: str
    twilio_to_whatsapp_env: str


@dataclass(frozen=True)
class NormalEurostarConfig:
    threshold_amount: float
    allowed_currencies: set[str]


@dataclass(frozen=True)
class AppConfig:
    settings: Settings
    notification: NotificationConfig
    checks: dict[str, bool]
    normal_eurostar: NormalEurostarConfig
    routes: list[RouteQuery]


def _parse_date(value: object) -> date:
    return date.fromisoformat(str(value))


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}. Copy config.example.yml to config.yml first.")

    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a YAML mapping.")

    settings_raw = raw.get("settings", {})
    notification_raw = raw.get("notification", {})
    normal_raw = raw.get("normal_eurostar", {})

    routes: list[RouteQuery] = []
    for r in raw.get("routes", []):
        passengers = int(r.get("passengers", 1))
        if not 1 <= passengers <= 4:
            raise ValueError(f"Snap supports 1-4 passengers. Route {r.get('name')} has passengers={passengers}.")
        start = _parse_date(r["start_date"])
        end = _parse_date(r["end_date"])
        if end < start:
            raise ValueError(f"Route {r.get('name')} has end_date before start_date.")
        routes.append(
            RouteQuery(
                name=str(r["name"]),
                origin=str(r["origin"]),
                destination=str(r["destination"]),
                start_date=start,
                end_date=end,
                passengers=passengers,
                snap_link=str(r.get("snap_link", "https://snap.eurostar.com/uk-en")),
                booking_link=str(r.get("booking_link", "https://www.eurostar.com/rw-en")),
            )
        )

    timezone = str(settings_raw.get("timezone", "Europe/London"))
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc

    checks = {k: bool(v) for k, v in raw.get("checks", {"snap": True, "normal_eurostar": True}).items()}
    if not any(checks.get(name, False) for name in ("snap", "normal_eurostar")):
        raise ValueError("Enable at least one check: snap or normal_eurostar.")
    if not routes:
        raise ValueError("Config must contain at least one route.")

    threshold = float(normal_raw.get("threshold_amount", 70))
    if threshold <= 0:
        raise ValueError("normal_eurostar.threshold_amount must be positive.")

    return AppConfig(
        settings=Settings(
            timezone=timezone,
            headless=bool(settings_raw.get("headless", True)),
            debug=bool(settings_raw.get("debug", True)),
            user_agent=str(settings_raw.get("user_agent", "")).strip(),
            snap_max_days_ahead=int(settings_raw.get("snap_max_days_ahead", 10)),
            page_timeout_ms=int(settings_raw.get("page_timeout_ms", 45000)),
        ),
        notification=NotificationConfig(
            provider=str(notification_raw.get("provider", "twilio_whatsapp")),
            twilio_account_sid_env=str(notification_raw.get("twilio_account_sid_env", "TWILIO_ACCOUNT_SID")),
            twilio_auth_token_env=str(notification_raw.get("twilio_auth_token_env", "TWILIO_AUTH_TOKEN")),
            twilio_from_whatsapp_env=str(notification_raw.get("twilio_from_whatsapp_env", "TWILIO_FROM_WHATSAPP")),
            twilio_to_whatsapp_env=str(notification_raw.get("twilio_to_whatsapp_env", "TWILIO_TO_WHATSAPP")),
        ),
        checks=checks,
        normal_eurostar=NormalEurostarConfig(
            threshold_amount=threshold,
            allowed_currencies={str(c).upper() for c in normal_raw.get("allowed_currencies", ["GBP", "EUR"])},
        ),
        routes=routes,
    )
