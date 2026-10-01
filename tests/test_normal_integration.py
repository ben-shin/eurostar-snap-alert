from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eurostar_alerts import scrapers
from eurostar_alerts.config import load_config
from eurostar_alerts.models import CheckStatus, Provider, RouteQuery
from eurostar_alerts.normal_transport import NormalTransportError
from eurostar_alerts.notifier import format_hit


@pytest.fixture
def normal_check(monkeypatch):
    config = load_config("config.yml")
    config = replace(config, normal_eurostar=replace(config.normal_eurostar, threshold_amount=200))
    route = RouteQuery("Brussels to London", "Brussels Midi", "London St Pancras",
                       date(2026, 10, 28), date(2026, 10, 28), booking_link="https://www.eurostar.com/uk-en")
    page = SimpleNamespace(url="https://www.eurostar.com/search/uk-en?origin=8814001&destination=7015400&adult=1&outbound=2026-10-28")
    monkeypatch.setattr(scrapers, "_open_and_fill", lambda *args: (object(), ""))
    monkeypatch.setattr(scrapers, "_submit_search", lambda *args: True)
    monkeypatch.setattr(scrapers, "_captcha_visible", lambda *args: False)
    monkeypatch.setattr(scrapers, "_body_text", lambda *args: "Sorry, something went wrong")
    monkeypatch.setattr(scrapers, "_save_debug", lambda *args: None)
    payload = json.loads((Path(__file__).parent / "fixtures/normal_new_booking_search.json").read_text())
    fetch = MagicMock(return_value=payload)
    monkeypatch.setattr(scrapers, "fetch_normal_response", fetch)
    return config, route, page, fetch


def test_normal_scan_uses_exact_selected_route_and_returns_paired_fare(normal_check):
    config, route, page, fetch = normal_check
    outcome = scrapers._check_normal(page, route, route.start_date, config)
    fetch.assert_called_once_with(origin_uic="8814001", destination_uic="7015400",
                                  travel_date=route.start_date, passengers=1, currency="GBP", market="uk")
    assert outcome.status == CheckStatus.AVAILABLE
    assert outcome.hit.provider == Provider.NORMAL
    assert outcome.hit.price_amount == 105
    assert (outcome.hit.departure_time, outcome.hit.arrival_time, outcome.hit.duration) == ("07:56", "08:57", "2h 01m")
    assert outcome.hit.fare_class == "Eurostar Standard"
    assert "Class: Eurostar Standard" in format_hit(outcome.hit)
    assert outcome.hit.dedupe_key == replace(outcome.hit, fare_class=None).dedupe_key


def test_above_threshold_is_successful_check_without_alert(normal_check):
    config, route, page, _ = normal_check
    config = replace(config, normal_eurostar=replace(config.normal_eurostar, threshold_amount=60))
    outcome = scrapers._check_normal(page, route, route.start_date, config)
    assert outcome.status == CheckStatus.UNAVAILABLE
    assert outcome.hit is None
    assert "105" in outcome.message


def test_transport_failure_remains_failed_not_unavailable(normal_check):
    config, route, page, fetch = normal_check
    fetch.side_effect = NormalTransportError("HTTP 403")
    outcome = scrapers._check_normal(page, route, route.start_date, config)
    assert outcome.status == CheckStatus.FAILED
    assert "403" in outcome.message


def test_response_route_mismatch_remains_failed(normal_check):
    config, route, page, fetch = normal_check
    fetch.return_value["data"]["journeySearch"]["outbound"]["origin"]["uic"] = "8727100"
    assert scrapers._check_normal(page, route, route.start_date, config).status == CheckStatus.FAILED


@pytest.mark.parametrize("blocked", ["captcha", "access denied", "unusual activity"])
def test_browser_security_block_prevents_alternate_request(normal_check, monkeypatch, blocked):
    config, route, page, fetch = normal_check
    monkeypatch.setattr(scrapers, "_captcha_visible", lambda *args: blocked == "captcha")
    monkeypatch.setattr(scrapers, "_body_text", lambda *args: blocked)
    assert scrapers._check_normal(page, route, route.start_date, config).status == CheckStatus.FAILED
    fetch.assert_not_called()


def test_wrong_search_date_prevents_fare_request(normal_check):
    config, route, page, fetch = normal_check
    page.url = page.url.replace("2026-10-28", "2026-10-29")
    assert scrapers._check_normal(page, route, route.start_date, config).status == CheckStatus.FAILED
    fetch.assert_not_called()
