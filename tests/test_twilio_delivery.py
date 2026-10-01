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
