from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest


from eurostar_alerts import main
from eurostar_alerts.config import load_config
from eurostar_alerts.control import ControlStore, effective_configs, seed_controls
from eurostar_alerts.models import CheckOutcome, CheckStatus, FareHit, Provider, ScrapeReport
from eurostar_alerts.status_report import format_check_report, make_check_report


@pytest.fixture(autouse=True)
def prepared_attempt(monkeypatch):
    monkeypatch.setattr(main, "prepare_check_report", lambda _, report: {
        "request_id": report["request_id"], "body": format_check_report(report),
        "from": "whatsapp:+100", "to": "whatsapp:+200",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


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
    assert store.publish_report(snapshot)
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
        assert store.publish_report(snapshot)
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
    failed_create = MagicMock(side_effect=main.CheckReportRejected("Twilio rejected creation"))
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
    assert store.publish_report(snapshot)
    assert snapshot["hit_count"] == 1
    assert len(snapshot["hits"]) == 1
    assert data["enabled"] is False


def test_created_message_is_reconciled_after_sid_persistence_failure(monkeypatch):
    store, request_id = _ready_delivery_store()
    message = SimpleNamespace(sid="SM" + "1" * 32, status="queued")
    def accepted(_config, attempt):
        persisted = store.document.data["check_request"]
        assert persisted["status"] == "sending"
        assert persisted["attempt"] == attempt
        return message
    create = MagicMock(side_effect=accepted)
    monkeypatch.setattr(main, "send_check_report", create)
    original_update = store.update_delivery
    def lost_write(*args, **kwargs):
        raise RuntimeError("Sync write unavailable after create succeeded")
    monkeypatch.setattr(store, "update_delivery", lost_write)
    with pytest.raises(RuntimeError, match="Sync write"):
        main._advance_check_delivery(store, store.config.notification)
    assert store.document.data["check_request"]["status"] == "sending"
    attempted_body = store.document.data["check_request"]["attempt"]["body"]
    monkeypatch.setattr(store, "update_delivery", original_update)
    reconcile = MagicMock(return_value=message)
    monkeypatch.setattr(main, "reconcile_check_report", reconcile)
    main._advance_check_delivery(store, store.config.notification)
    assert create.call_count == 1
    assert reconcile.call_args.args[1]["body"] == attempted_body
    assert store.document.data["check_request"]["pending_sid"] == message.sid
    assert store.document.data["check_request"]["status"] == "delivery_pending"


def test_ambiguous_create_never_blindly_retries(monkeypatch):
    store, request_id = _ready_delivery_store()
    create = MagicMock(side_effect=TimeoutError("response lost"))
    monkeypatch.setattr(main, "send_check_report", create)
    with pytest.raises(RuntimeError, match="outcome uncertain"):
        main._advance_check_delivery(store, store.config.notification)
    assert store.document.data["check_request"]["status"] == "sending"
    reconcile = MagicMock(return_value=None)
    monkeypatch.setattr(main, "reconcile_check_report", reconcile)
    with pytest.raises(RuntimeError, match="nothing was resent"):
        main._advance_check_delivery(store, store.config.notification)
    assert create.call_count == 1
    assert store.document.data["check_request"]["status"] == "sending"
    reconcile.return_value = SimpleNamespace(sid="SM" + "2" * 32, status="read")
    main._advance_check_delivery(store, store.config.notification)
    assert create.call_count == 1
    assert store.document.data["check_request"]["status"] == "delivered"


@pytest.mark.parametrize("previous", ["report_ready", "sending", "delivery_pending"])
def test_cancelled_delivery_stops_every_later_worker_action(monkeypatch, previous):
    store, request_id = _ready_delivery_store()
    store.document.data["check_request"].update(status="cancelled", cancelled_after=previous,
                                               attempt={"body": "preserved"}, pending_sid="SMold")
    create, reconcile, status = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(main, "send_check_report", create)
    monkeypatch.setattr(main, "reconcile_check_report", reconcile)
    monkeypatch.setattr(main, "check_report_delivery_status", status)
    main._advance_check_delivery(store, store.config.notification)
    create.assert_not_called()
    reconcile.assert_not_called()
    status.assert_not_called()
    assert store.document.data["check_request"]["attempt"]["body"] == "preserved"


def test_cancel_after_claim_but_before_create_suppresses_unsent_message(monkeypatch):
    store, request_id = _ready_delivery_store()
    original_begin = store.begin_delivery
    def cancel_after_claim(*args):
        assert original_begin(*args)
        store.document.data["check_request"]["status"] = "cancelled"
        return True
    monkeypatch.setattr(store, "begin_delivery", cancel_after_claim)
    create = MagicMock()
    monkeypatch.setattr(main, "send_check_report", create)
    main._advance_check_delivery(store, store.config.notification)
    create.assert_not_called()


def test_snapshot_only_zero_eligible_publishes_honest_paused_report(monkeypatch, tmp_path):
    data = seed_controls(load_config("config.yml"))
    data["enabled"] = False
    for search in data["searches"]:
        search["enabled"] = False
    store = store_with(data)
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    scan, send = MagicMock(), MagicMock()
    monkeypatch.setattr(main, "run_with_browser", scan)
    monkeypatch.setattr(main, "send_check_report", send)
    state = tmp_path / "state.json"
    assert main.main(["--controls", str(metadata), "--snapshot-only", "--state", str(state),
                      "--report", str(tmp_path / "report.json")]) == 0
    report = store.document.data["check_report"]
    assert report["state"] == "paused"
    assert report["attempted"] == report["hit_count"] == 0
    assert report["manual_snapshot"] is True
    assert store.document.data["enabled"] is False
    assert not state.exists()
    scan.assert_not_called()
    send.assert_not_called()


def test_snapshot_only_rejected_publication_is_not_success(monkeypatch, tmp_path, capsys):
    store, _ = _ready_delivery_store()
    for search in store.document.data["searches"]:
        search["enabled"] = False
    before = deepcopy(store.document.data["check_report"])
    metadata = tmp_path / "connection.json"
    metadata.write_text("{}")
    monkeypatch.setattr(main, "ControlStore", lambda *args: store)
    with pytest.raises(RuntimeError, match="snapshot was not published"):
        main.main(["--controls", str(metadata), "--snapshot-only", "--report", str(tmp_path / "report.json")])
    assert store.document.data["check_report"] == before
    assert "Results snapshot published" not in capsys.readouterr().out


def test_durable_attempt_keeps_exact_unicode_body_within_sync_limit():
    import json
    store, request_id = _ready_delivery_store()
    report = store.document.data["check_report"]
    record = next(search for search in report["searches"] if search["status"] == "active")
    record["hit_count"] = report["hit_count"] = 1
    report["hits"] = [{"search_id": record["id"], "booking_url": "x" * 9000}]
    attempt = {"request_id": request_id, "body": "旅" * 2000,
               "from": "whatsapp:+100", "to": "whatsapp:+200",
               "started_at": datetime.now(timezone.utc).isoformat()}
    assert store.begin_delivery(request_id, deepcopy(report), attempt)
    assert len(json.dumps(store.document.data).encode("utf-8")) <= 16_000
    assert store.document.data["check_request"]["attempt"]["body"] == "旅" * 2000
    assert store.document.data["check_report"]["omitted_hits"] == 1


def test_history_reconciliation_includes_queued_messages_and_matches_exact_attempt(monkeypatch):
    from eurostar_alerts import notifier
    now = datetime.now(timezone.utc)
    attempt = {"request_id": "SMrequest", "body": "exact attempted report", "from": "whatsapp:+100",
               "to": "whatsapp:+200", "started_at": now.isoformat(), "failed_sids": ["SMfailed"]}
    queued = SimpleNamespace(sid="SMqueued", status="queued", body=attempt["body"],
                             from_=attempt["from"], to=attempt["to"], date_created=now, date_sent=None)
    wrong_to = SimpleNamespace(**{**vars(queued), "sid": "SMother", "to": "whatsapp:+300"})
    old = SimpleNamespace(**{**vars(queued), "sid": "SMold", "date_created": now - timedelta(days=1)})
    failed = SimpleNamespace(**{**vars(queued), "sid": "SMfailed", "status": "failed"})
    client = MagicMock()
    client.messages.list.return_value = [wrong_to, old, failed, queued]
    monkeypatch.setattr(notifier, "twilio_client", lambda _: (client, attempt["from"], attempt["to"]))
    assert notifier.reconcile_check_report(load_config("config.yml").notification, attempt) is queued
    client.messages.list.assert_called_once_with(from_=attempt["from"], to=attempt["to"], limit=200)
