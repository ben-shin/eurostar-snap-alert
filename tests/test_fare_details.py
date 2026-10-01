from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from eurostar_alerts import notifier
from eurostar_alerts.config import load_config
from eurostar_alerts.models import FareHit, Provider
from eurostar_alerts.scrapers import _normal_journey_details, _snap_price_and_window
from eurostar_alerts.state import AlertState


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            yield page
        finally:
            browser.close()


def snap_hit(**details):
    return FareHit(
        Provider.SNAP, "Brussels to London", "Brussels Midi", "London St Pancras",
        date(2026, 10, 3), 1, 45, "GBP",
        "https://snap.eurostar.com/uk-en/search?outbound=2026-10-03",
        "Exact-date Snap availability found.",
        **details,
    )


def test_live_snap_markup_keeps_winning_price_with_its_window_and_excludes_adjacent_date(page):
    # Reduced from the live Brussels -> London result on 2026-10-03.
    page.set_content("""
      <span data-testid="calendarDay-price">£35</span>
      <div data-testid="2026-10-03-outbound-07:56">
        <span>Leaving between</span><p>07:56 - 14:00</p>
        <div data-testid="2026-10-03-outbound-07:56-price">£55</div>
      </div>
      <div data-testid="2026-10-03-outbound-14:00">
        <span>Leaving between</span><p>14:00 - 20:26</p>
        <div data-testid="2026-10-03-outbound-14:00-price">£45</div>
      </div>
      <div data-testid="2026-10-04-outbound-09:00">
        <span>Leaving between</span><p>09:00 - 14:00</p>
        <div data-testid="2026-10-04-outbound-09:00-price">£25</div>
      </div>
    """)
    prices = page.locator('[data-testid^="2026-10-03-outbound-"][data-testid$="-price"]')
    assert _snap_price_and_window(prices) == ("GBP", 45.0, "14:00-20:26")


def test_snap_missing_window_is_not_invented(page):
    page.set_content('<div data-testid="2026-10-03-outbound-14:00-price">£45</div>')
    prices = page.locator('[data-testid^="2026-10-03-outbound-"][data-testid$="-price"]')
    assert _snap_price_and_window(prices) == ("GBP", 45.0, None)
    message = notifier.format_hit(snap_hit())
    assert "Departure/arrival: assigned by Eurostar; not available at booking" in message
    assert "Departure window:" not in message


def test_normal_times_must_belong_to_winning_price_card(page):
    page.set_content("""
      <div data-testid="journey-card">
        <span data-testid="departure-time">07:56</span>
        <span data-testid="arrival-time">09:57</span>
        <span data-testid="duration">3h 01m</span>
        <span data-testid="journey-price">£65</span>
      </div>
      <div data-testid="journey-card">
        <span data-testid="departure-time">14:00</span>
        <span data-testid="arrival-time">16:05</span>
        <span data-testid="duration">3h 05m</span>
        <span data-testid="journey-price">£45</span>
      </div>
    """)
    assert _normal_journey_details(page, ("GBP", 45.0)) == ("14:00", "16:05", "3h 05m")
    assert _normal_journey_details(page, ("GBP", 65.0)) == ("07:56", "09:57", "3h 01m")


def test_normal_times_missing_or_ambiguous_remain_unavailable(page):
    page.set_content("""
      <div data-testid="journey-card"><span data-testid="departure-time">07:56</span>
        <span data-testid="arrival-time">09:57</span><span data-testid="journey-price">£45</span></div>
      <div data-testid="journey-card"><span data-testid="departure-time">14:00</span>
        <span data-testid="arrival-time">16:05</span><span data-testid="journey-price">£45</span></div>
    """)
    assert _normal_journey_details(page, ("GBP", 45.0)) == (None, None, None)
    page.set_content('<span data-testid="journey-price">£45</span><span>07:56 to 09:57</span>')
    assert _normal_journey_details(page, ("GBP", 45.0)) == (None, None, None)


def test_alerts_label_local_times_and_preserve_dedupe_key():
    snap = snap_hit(departure_window="14:00-20:26")
    assert "Departure window: 14:00-20:26 (Brussels Midi local time)" in notifier.format_hit(snap)
    assert snap.dedupe_key == snap_hit().dedupe_key
    normal = replace(
        snap, provider=Provider.NORMAL, departure_window=None,
        departure_time="14:00", arrival_time="16:05", duration="3h 05m",
    )
    message = notifier.format_hit(normal)
    assert "Departure: 14:00 (Brussels Midi local time)" in message
    assert "Arrival: 16:05 (London St Pancras local time)" in message
    assert "Duration: 3h 05m" in message


def test_legacy_failed_body_is_still_restored(monkeypatch, tmp_path):
    hit = snap_hit()
    state = AlertState(tmp_path / "state.json")
    state.mark_seen([hit])
    config = load_config("config.yml")
    client = MagicMock()
    client.messages.list.return_value = [
        SimpleNamespace(
            body=notifier.format_hit(hit, include_details=False),
            status="failed",
            date_created=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
    ]
    monkeypatch.setattr(notifier, "twilio_client", lambda _: (client, "from", "to"))
    notifier.restore_failed_alerts(config.notification, [hit], state)
    assert state.unseen([hit]) == [hit]

