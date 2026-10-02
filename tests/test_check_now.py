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
    store.delivery_state.return_value = None
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
    send.assert_not_called()  # Delivery is exercised separately from the scan.
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
    store.delivery_state.return_value = None
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
    report["errors"] = [{"search_id": data["searches"][0]["id"], "provider": "snap",
                         "date": "2026-10-02", "message": "internal /secret/path and traceback"}]
    assert store.publish_report(report, request_id)
    assert store.document.data["check_request"]["status"] == "report_ready"
    assert store.document.data["check_report"]["request_id"] == request_id
    assert not store.publish_report(report, request_id)
    message = format_check_report(report)
    assert "Some fare checks failed" in message
    assert "/secret/path" not in message
    assert len(message) <= 1500


def _ready_delivery_store():
    config = load_config("config.yml")
    data = seed_controls(config)
    request_id = "SM" + "c" * 32
    data["check_request"] = {"id": request_id, "status": "report_ready",
                             "started_at": datetime.now(timezone.utc).isoformat()}
    data["check_report"] = make_check_report(
        data, [], request_id=request_id, started_at=datetime.now(timezone.utc).isoformat()
    )
    return store_with(data), request_id


def test_zero_hit_report_marks_changed_threshold_and_new_search():
    config = load_config("config.yml")
    data = seed_controls(config)
    original = deepcopy(data)
    snapshot = make_check_report(original, [(original["searches"][0], ScrapeReport([]))],
                                 request_id=None, started_at=datetime.now(timezone.utc).isoformat())
    data["searches"][0]["max"] = 30
    added = deepcopy(data["searches"][0])
    added["id"] = "s3"
    data["searches"].append(added)
    store = store_with(data)
    assert not store.publish_report(snapshot)
    assert snapshot["coverage_changed"]
    assert snapshot["state"] == "partial"
    assert any("s3" in note for note in snapshot["coverage_notes"])
    assert "No travel dates" not in format_check_report(snapshot)


def test_publication_suppresses_hit_after_midscan_edit_or_pause():
    config = load_config("config.yml")
    data = seed_controls(config)
    search = deepcopy(data["searches"][0])
    hit = FareHit(Provider.NORMAL, search["name"], search["origin"], search["destination"],
                  config.routes[0].start_date, 1, 55, "GBP", "https://www.eurostar.com/search",
                  "test", "07:56", "08:57")
    run = ScrapeReport([CheckOutcome(Provider.NORMAL, search["name"], hit.travel_date,
                                    CheckStatus.AVAILABLE, "55", hit)])
    for change in ({"max": 30}, {"enabled": False}):
        changed = deepcopy(data)
        changed["searches"][0].update(change)
        snapshot = make_check_report(data, [(search, run)], request_id=None,
                                     started_at=datetime.now(timezone.utc).isoformat())
        store = store_with(changed)
        assert not store.publish_report(snapshot)
        assert snapshot["coverage_changed"]
        assert snapshot["state"] == "partial"
        assert snapshot["hit_count"] == 0
        assert snapshot["hits"] == []


def test_interrupted_before_first_date_is_not_a_clear_or_booking_window_result():
    config = load_config("config.yml")
    data = seed_controls(config)
    snapshot = make_check_report(data, [(data["searches"][0], ScrapeReport([]))],
                                 request_id=None, started_at=datetime.now(timezone.utc).isoformat(),
                                 interrupted_ids={data["searches"][0]["id"]})
    assert snapshot["state"] == "partial"
    assert snapshot["coverage_changed"]
    message = format_check_report(snapshot)
    assert "interrupted" in message
    assert "No travel dates" not in message


def test_completion_create_failure_retries_saved_report_without_rescan(monkeypatch):
    store, request_id = _ready_delivery_store()
    failed_create = MagicMock(side_effect=RuntimeError("Twilio unavailable"))
    monkeypatch.setattr(main, "send_check_report", failed_create)
    try:
        main._advance_check_delivery(store, store.config.notification)
        assert False, "Expected definite create failure"
    except RuntimeError:
        pass
    assert store.document.data["check_request"]["status"] == "report_ready"
    assert store.document.data["check_report"]["request_id"] == request_id
    created = SimpleNamespace(sid="SM" + "d" * 32, status="queued")
    failed_create.side_effect = None
    failed_create.return_value = created
    main._advance_check_delivery(store, store.config.notification)
    assert store.document.data["check_request"]["pending_sid"] == created.sid
    assert store.document.data["check_request"]["status"] == "delivery_pending"
    assert failed_create.call_count == 2


def test_pending_delivery_polls_then_failed_sid_recovers(monkeypatch):
    store, request_id = _ready_delivery_store()
    sid = "SM" + "e" * 32
    assert store.update_delivery(request_id, "report_ready", "delivery_pending", pending_sid=sid)
    create = MagicMock()
    fetch = MagicMock(return_value="sent")
    monkeypatch.setattr(main, "send_check_report", create)
    monkeypatch.setattr(main, "check_report_delivery_status", fetch)
    main._advance_check_delivery(store, store.config.notification)
    assert store.document.data["check_request"]["status"] == "delivery_pending"
    create.assert_not_called()
    fetch.return_value = "failed"
    main._advance_check_delivery(store, store.config.notification)
    assert store.document.data["check_request"]["status"] == "report_ready"
    create.return_value = SimpleNamespace(sid="SM" + "f" * 32, status="delivered")
    main._advance_check_delivery(store, store.config.notification)
    assert store.document.data["check_request"]["status"] == "delivered"
    assert create.call_count == 1


def test_snapshot_only_writes_real_report_without_phone_or_dedupe_side_effects(monkeypatch, tmp_path):
    config = load_config("config.yml")
    data = seed_controls(config)
    data["enabled"] = False
    data["searches"][0]["start_date"] = "2099-10-01"
    data["searches"][0]["end_date"] = "2099-10-05"
    data["searches"][1]["enabled"] = False
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    store = MagicMock()
    store.read.return_value = data
    store.still_active.return_value = True
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    calls = []
    def scan(_config, *, should_continue):
        calls.append(should_continue())
        return ScrapeReport([])
    monkeypatch.setattr(main, "run_with_browser", scan)
    send = MagicMock()
    alerts = MagicMock()
    monkeypatch.setattr(main, "send_check_report", send)
    monkeypatch.setattr(main, "send_whatsapp_hits", alerts)
    state = tmp_path / "notified.json"
    assert main.main(["--controls", str(metadata), "--state", str(state),
                      "--report", str(tmp_path / "report.json"), "--snapshot-only"]) == 0
    assert calls == [True]
    assert data["enabled"] is False
    assert not state.exists()
    store.claim_request.assert_not_called()
    store.publish_report.assert_called_once()
    store.delivery_state.assert_not_called()
    send.assert_not_called()
    alerts.assert_not_called()


def test_dry_run_does_not_retry_saved_completion_delivery(monkeypatch, tmp_path):
    config = load_config("config.yml")
    data = seed_controls(config)
    data["enabled"] = False
    data["check_request"] = {"id": "SM" + "a" * 32, "status": "report_ready"}
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    store = MagicMock()
    store.read.return_value = data
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    send = MagicMock()
    monkeypatch.setattr(main, "send_check_report", send)
    assert main.main(["--controls", str(metadata), "--state", str(tmp_path / "notified.json"),
                      "--report", str(tmp_path / "report.json"), "--dry-run"]) == 0
    store.claim_request.assert_not_called()
    store.publish_report.assert_not_called()
    store.delivery_state.assert_not_called()
    send.assert_not_called()


def test_snapshot_only_keeps_valid_hits_while_automatic_alerts_are_paused():
    config = load_config("config.yml")
    data = seed_controls(config)
    data["enabled"] = False
    search = data["searches"][0]
    hit = FareHit(Provider.NORMAL, search["name"], search["origin"], search["destination"],
                  config.routes[0].start_date, 1, 55, "GBP", "https://www.eurostar.com/search",
                  "test", "07:56", "08:57")
    run = ScrapeReport([CheckOutcome(Provider.NORMAL, search["name"], hit.travel_date,
                                    CheckStatus.AVAILABLE, "55", hit)])
    snapshot = make_check_report(data, [(search, run)], request_id=None,
                                 started_at=datetime.now(timezone.utc).isoformat(),
                                 manual_snapshot=True)
    store = store_with(data)
    assert not store.publish_report(snapshot)
    assert snapshot["hit_count"] == 1
    assert len(snapshot["hits"]) == 1
    assert data["enabled"] is False
