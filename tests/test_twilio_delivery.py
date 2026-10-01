from types import SimpleNamespace

import pytest

from eurostar_alerts.test_twilio import delivery_result


@pytest.mark.parametrize("status,success", [
    ("delivered", True), ("read", True), ("queued", False), ("sent", False),
    ("failed", False), ("undelivered", False),
])
def test_acceptance_is_not_delivery(status, success):
    message = SimpleNamespace(status=status, error_code=63016)
    assert delivery_result(message)[0] is success


def test_closed_whatsapp_window_has_actionable_guidance():
    message = SimpleNamespace(status="undelivered", error_code=63016)
    ok, details = delivery_result(message)
    assert not ok
    assert "Send TEST" in details
    assert "24-hour" in details

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from eurostar_alerts import notifier
from eurostar_alerts.config import load_config
from eurostar_alerts.state import AlertState
from test_state_and_messages import sample_hit


def test_failed_delivery_is_never_marked_seen(monkeypatch, tmp_path):
    config = load_config("config.yml")
    state = AlertState(tmp_path / "state.json")
    hit = sample_hit()
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(sid="SMfailed", status="failed", error_code=63015)
    monkeypatch.setattr(notifier, "twilio_client", lambda _: (client, "from", "to"))
    with pytest.raises(RuntimeError, match="Rejoin"):
        notifier.send_whatsapp_hits(config.notification, [hit], state)
    assert state.unseen([hit]) == [hit]
    assert state.pending_sid(hit) is None


def test_pending_delivery_is_reused_then_marked_seen(monkeypatch, tmp_path):
    config = load_config("config.yml")
    state = AlertState(tmp_path / "state.json")
    hit = sample_hit()
    state.record_pending(hit, "SMpending")
    state = AlertState(state.path)
    client = MagicMock()
    client.messages.return_value.fetch.return_value = SimpleNamespace(sid="SMpending", status="delivered")
    monkeypatch.setattr(notifier, "twilio_client", lambda _: (client, "from", "to"))
    notifier.send_whatsapp_hits(config.notification, [hit], state)
    client.messages.create.assert_not_called()
    assert state.unseen([hit]) == []
    assert state.pending_sid(hit) is None


def test_unconfirmed_message_is_persisted_without_resending(monkeypatch, tmp_path):
    config = load_config("config.yml")
    state = AlertState(tmp_path / "state.json")
    hit = sample_hit()
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(sid="SMqueued", status="queued")
    monkeypatch.setattr(notifier, "twilio_client", lambda _: (client, "from", "to"))
    times = iter([0, 21])
    monkeypatch.setattr(notifier.time, "monotonic", lambda: next(times))
    notifier.send_whatsapp_hits(config.notification, [hit], state)
    assert AlertState(state.path).pending_sid(hit) == "SMqueued"
    assert state.unseen([hit]) == [hit]


def test_repair_only_removes_proven_failed_alerts(monkeypatch, tmp_path):
    config = load_config("config.yml")
    state = AlertState(tmp_path / "state.json")
    hit = sample_hit()
    state.mark_seen([hit])
    client = MagicMock()
    client.messages.list.return_value = [
        SimpleNamespace(body=notifier.format_hit(hit), status="failed", date_created=datetime(2026, 10, 1, tzinfo=timezone.utc)),
        SimpleNamespace(body="unrelated message", status="failed", date_created=datetime(2026, 10, 1, tzinfo=timezone.utc)),
    ]
    monkeypatch.setattr(notifier, "twilio_client", lambda _: (client, "from", "to"))
    notifier.restore_failed_alerts(config.notification, [hit], state)
    assert state.unseen([hit]) == [hit]
    state.mark_seen([hit])
    client.messages.list.return_value.append(
        SimpleNamespace(body=notifier.format_hit(hit), status="delivered",
                        date_created=datetime(2026, 10, 2, tzinfo=timezone.utc))
    )
    notifier.restore_failed_alerts(config.notification, [hit], state)
    assert state.unseen([hit]) == []
