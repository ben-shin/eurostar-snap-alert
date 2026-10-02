from copy import deepcopy
from datetime import date
from unittest.mock import MagicMock

from eurostar_alerts.config import load_config
from eurostar_alerts.control import ControlStore, effective_configs, seed_controls
from eurostar_alerts import main, scrapers


def controls():
    config = load_config("config.yml")
    data = seed_controls(config)
    data["searches"][0].update(start_date="2026-10-01", end_date="2026-10-05")
    data["searches"][1].update(start_date="2026-10-01", end_date="2026-10-10")
    return config, data


def test_expiry_is_per_search_and_end_date_is_inclusive():
    config, data = controls()
    assert len(effective_configs(config, data, date(2026, 10, 5))) == 2
    assert [s["id"] for s, _ in effective_configs(config, data, date(2026, 10, 6))] == ["s2"]
    assert effective_configs(config, data, date(2026, 10, 11)) == []


def test_pause_and_per_search_parameters():
    config, data = controls()
    data["searches"][0].update(enabled=False)
    data["searches"][1].update(mode="both", passengers=3, max=42)
    [(search, effective)] = effective_configs(config, data, date(2026, 10, 1))
    assert search["id"] == "s2"
    assert effective.routes[0].passengers == 3
    assert effective.normal_eurostar.threshold_amount == 42
    assert effective.checks == {"snap": True, "normal_eurostar": True}
    data["enabled"] = False
    assert effective_configs(config, data, date(2026, 10, 1)) == []


def test_changed_or_paused_search_cannot_send_stale_fares(monkeypatch):
    config, data = controls()
    original = deepcopy(data["searches"][0])
    store = object.__new__(ControlStore)
    store.config = config
    store.read = lambda: data
    monkeypatch.setattr("eurostar_alerts.control.effective_configs",
                        lambda config, data, **kwargs: [(s, config) for s in data["searches"]] if data["enabled"] else [])
    assert store.still_active(original)
    data["searches"][0]["passengers"] = 2
    assert not store.still_active(original)
    data["searches"][0] = original
    data["enabled"] = False
    assert not store.still_active(original)


def test_expired_dates_do_not_launch_browser(monkeypatch):
    config = load_config("config.example.yml")
    from dataclasses import replace
    config = replace(config, routes=[replace(config.routes[0], start_date=date(2000, 1, 1), end_date=date(2000, 1, 2))])
    browser = MagicMock()
    monkeypatch.setattr(scrapers, "sync_playwright", browser)
    assert scrapers.run_with_browser(config).attempted == 0
    browser.assert_not_called()


def test_paused_monitor_does_not_scan_or_notify(monkeypatch, tmp_path):
    config, data = controls()
    data["enabled"] = False
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    store = MagicMock()
    store.read.return_value = data
    store.claim_request.return_value = None
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    scan = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(main, "run_with_browser", scan)
    monkeypatch.setattr(main, "send_whatsapp_hits", send)
    assert main.main(["--controls", str(metadata), "--state", str(tmp_path / "state.json"),
                      "--report", str(tmp_path / "report.json")]) == 0
    scan.assert_not_called()
    send.assert_not_called()


def test_stop_during_snap_run_prevents_next_date(monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from eurostar_alerts.models import CheckOutcome, CheckStatus, Provider
    config = load_config("config.example.yml")
    today = datetime.now(timezone.utc).date()
    from datetime import timedelta
    config = replace(config, checks={"snap": True, "normal_eurostar": False},
                     routes=[replace(config.routes[0], start_date=today + timedelta(days=1),
                                     end_date=today + timedelta(days=3))])
    monkeypatch.setattr(scrapers, "sync_playwright", MagicMock())
    calls = []
    def check(page, route, travel_date, cfg):
        calls.append(travel_date)
        return CheckOutcome(Provider.SNAP, route.name, travel_date, CheckStatus.UNAVAILABLE, "no fares")
    monkeypatch.setattr(scrapers, "_check_snap", check)
    report = scrapers.run_with_browser(config, should_continue=lambda: not calls)
    assert len(calls) == 1
    assert report.attempted == 1


def test_changed_search_suppresses_alert_and_dedupe_update(monkeypatch, tmp_path):
    from eurostar_alerts.models import CheckOutcome, CheckStatus, FareHit, Provider, ScrapeReport
    config, data = controls()
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}")
    store = MagicMock()
    store.read.return_value = data
    store.claim_request.return_value = None
    store.still_active.return_value = False
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    monkeypatch.setattr(main, "effective_configs", lambda *args, **kwargs: [(data["searches"][0], config)])
    hit = FareHit(Provider.SNAP, "test route", "Brussels Midi", "London St Pancras",
                  date(2026, 10, 2), 1, 45, "GBP", "https://snap.eurostar.com/", "test")
    report = ScrapeReport([CheckOutcome(Provider.SNAP, "test route", hit.travel_date, CheckStatus.AVAILABLE, "test", hit)])
    monkeypatch.setattr(main, "run_with_browser", lambda *args, **kwargs: report)
    send = MagicMock()
    monkeypatch.setattr(main, "send_whatsapp_hits", send)
    state = tmp_path / "state.json"
    assert main.main(["--controls", str(metadata), "--state", str(state),
                      "--report", str(tmp_path / "report.json")]) == 0
    send.assert_not_called()
    assert not state.exists()


def test_dry_run_does_not_create_notification_state(monkeypatch, tmp_path):
    from eurostar_alerts.models import ScrapeReport
    monkeypatch.setattr(main, "run_with_browser", lambda *args: ScrapeReport([]))
    state = tmp_path / "state.json"
    assert main.main(["--dry-run", "--local-config", "--state", str(state),
                      "--report", str(tmp_path / "report.json")]) == 0
    assert not state.exists()
