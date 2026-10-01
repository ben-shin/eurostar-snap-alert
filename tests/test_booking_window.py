from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from eurostar_alerts import scrapers
from eurostar_alerts.config import load_config
from eurostar_alerts.models import CheckOutcome, CheckStatus, Provider, RouteQuery


@pytest.mark.parametrize(
    "instant, today",
    [
        (datetime(2026, 10, 1, 13, tzinfo=timezone.utc), date(2026, 10, 1)),
        # The booking window rolls over at London midnight, not UTC midnight.
        (datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc), date(2026, 10, 2)),
    ],
)
def test_provider_date_windows(monkeypatch, instant, today):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(scrapers, "datetime", FixedDateTime)
    monkeypatch.setattr(scrapers, "sync_playwright", MagicMock())
    config = load_config("config.example.yml")
    route = RouteQuery(
        "Brussels to London", "Brussels Midi", "London St Pancras",
        date(2026, 9, 30), date(2026, 10, 13),
    )
    config = replace(
        config,
        settings=replace(config.settings, timezone="Europe/London", snap_max_days_ahead=10),
        checks={"snap": True, "normal_eurostar": True},
        routes=[route],
    )
    checked = {Provider.SNAP: [], Provider.NORMAL: []}

    def checker(provider):
        def check(page, route, travel_date, config):
            checked[provider].append(travel_date)
            # Real scrape failures must still appear in the health report.
            status = CheckStatus.FAILED if travel_date == date(2026, 10, 5) else CheckStatus.UNAVAILABLE
            return CheckOutcome(provider, route.name, travel_date, status, "test result")
        return check

    monkeypatch.setattr(scrapers, "_check_snap", checker(Provider.SNAP))
    monkeypatch.setattr(scrapers, "_check_normal", checker(Provider.NORMAL))

    report = scrapers.run_with_browser(config)

    assert checked[Provider.SNAP] == [date(2026, 10, day) for day in range(today.day + 1, today.day + 11)]
    assert checked[Provider.NORMAL] == [date(2026, 10, day) for day in range(today.day, 14)]
    assert report.attempted == len(checked[Provider.SNAP]) + len(checked[Provider.NORMAL])
    assert len(report.failures) == 2
