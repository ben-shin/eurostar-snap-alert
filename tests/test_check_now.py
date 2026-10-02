from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from eurostar_alerts import main
from eurostar_alerts.config import load_config
from eurostar_alerts.control import ControlStore, effective_configs, seed_controls
from eurostar_alerts.models import CheckOutcome, CheckStatus, FareHit, Provider, ScrapeReport
from eurostar_alerts.status_report import format_check_report, make_check_report


class Document:
    def __init__(self, data):
        self.data = deepcopy(data)
        self.revision = "1"
        self.updates = 0

    def fetch(self):
        return SimpleNamespace(data=deepcopy(self.data), revision=self.revision)

    def update(self, *, data, if_match):
        assert if_match == self.revision
        self.data = deepcopy(data)
        self.updates += 1
        self.revision = str(self.updates + 1)


def store_with(data):
    store = object.__new__(ControlStore)
    store.config = load_config("config.yml")
    store.document = Document(data)
    return store


def test_claim_recovery_and_cancellation_use_request_identity():
    config = load_config("config.yml")
    data = seed_controls(config)
    data["enabled"] = False
    data["searches"][0]["start_date"] = "2099-10-01"
    data["searches"][0]["end_date"] = "2099-10-05"
    data["check_request"] = {
        "id": "SM" + "a" * 32, "status": "running",
        "started_at": (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat(),
    }
    store = store_with(data)
    assert effective_configs(config, data) == []
    assert effective_configs(config, data, allow_global_pause=True)
    assert store.claim_request() == data["check_request"]["id"]
    original = deepcopy(data["searches"][0])
    assert store.still_active(original, data["check_request"]["id"])
    assert store.claim_request() is None
    del store.document.data["check_request"]
    assert not store.still_active(original, data["check_request"]["id"])


def test_snapshot_is_current_and_independent_of_alert_state():
    config = load_config("config.yml")
    data = seed_controls(config)
    search = data["searches"][0]
    hit = FareHit(
        Provider.NORMAL, search["name"], search["origin"], search["destination"],
        config.routes[0].start_date, search["passengers"], 55, "GBP",
        "https://www.eurostar.com/search", "test", "07:56", "08:57",
        fare_class="Eurostar Standard",
    )
    report = ScrapeReport([
        CheckOutcome(Provider.NORMAL, search["name"], hit.travel_date, CheckStatus.AVAILABLE, "55", hit),
        CheckOutcome(Provider.SNAP, search["name"], hit.travel_date, CheckStatus.FAILED, "blocked"),
    ])
    snapshot = make_check_report(data, [(search, report)], request_id="SM" + "a" * 32, started_at=datetime.now(timezone.utc).isoformat())
    assert snapshot["state"] == "partial"
    assert (snapshot["attempted"], snapshot["completed"], snapshot["failed"], snapshot["hit_count"]) == (2, 1, 1, 1)
    assert snapshot["hits"][0]["departure_time"] == "07:56"
    assert snapshot["errors"][0]["message"] == "blocked"
    assert snapshot["searches"][0]["settings"]["mode"] == search["mode"]


def test_one_off_when_automatic_monitoring_paused_keeps_dedupe_untouched(monkeypatch, tmp_path):
    config = load_config("config.yml")
    data = seed_controls(config)
    data["enabled"] = False
    data["searches"][0]["start_date"] = "2099-10-01"
    data["searches"][0]["end_date"] = "2099-10-05"
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    store = MagicMock()
    store.read.return_value = data
    store.claim_request.return_value = "SM" + "a" * 32
    store.publish_report.return_value = True
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    monkeypatch.setattr(main, "run_with_browser", lambda *args, **kwargs: ScrapeReport([]))
    send = MagicMock()
    monkeypatch.setattr(main, "send_check_report", send)
    alerts = MagicMock()
    monkeypatch.setattr(main, "send_whatsapp_hits", alerts)
    state = tmp_path / "notified.json"
    assert main.main(["--controls", str(metadata), "--state", str(state),
                      "--report", str(tmp_path / "report.json")]) == 0
    assert data["enabled"] is False
    assert not state.exists()
    assert store.publish_report.called
    assert send.call_count == 1
    alerts.assert_not_called()


def test_skipped_paused_cron_retains_previous_snapshot(monkeypatch, tmp_path):
    config = load_config("config.yml")
    data = seed_controls(config)
    data["enabled"] = False
    data["check_report"] = {"hit_count": 1, "completed_at": "2026-10-01T12:00:00Z"}
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    store = MagicMock()
    store.read.return_value = data
    store.claim_request.return_value = None
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    monkeypatch.setattr(main, "run_with_browser", MagicMock())
    assert main.main(["--controls", str(metadata), "--state", str(tmp_path / "notified.json"),
                      "--report", str(tmp_path / "report.json")]) == 0
    store.publish_report.assert_not_called()
    assert data["check_report"]["hit_count"] == 1


def test_completed_request_is_published_once_and_message_hides_internal_error():
    config = load_config("config.yml")
    data = seed_controls(config)
    request_id = "SM" + "b" * 32
    data["check_request"] = {"id": request_id, "status": "running",
                             "started_at": datetime.now(timezone.utc).isoformat()}
    store = store_with(data)
    report = make_check_report(data, [], request_id=request_id,
                               started_at=datetime.now(timezone.utc).isoformat())
    report["state"] = "failed"
    report["attempted"] = 1
    report["failed"] = 1
    report["errors"] = [{"message": "internal /secret/path and traceback"}]
    assert store.publish_report(report, request_id)
    assert store.document.data["check_request"]["status"] == "completed"
    assert store.document.data["check_report"]["request_id"] == request_id
    assert not store.publish_report(report, request_id)
    message = format_check_report(report)
    assert "Some fare checks failed" in message
    assert "/secret/path" not in message
    assert len(message) <= 1500
