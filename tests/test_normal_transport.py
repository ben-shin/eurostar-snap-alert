from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import subprocess

import pytest

from eurostar_alerts import normal_transport
from eurostar_alerts.normal_transport import NormalTransportError


def fetch(**overrides):
    kwargs = dict(origin_uic="8814001", destination_uic="7015400", travel_date=date(2026, 10, 28),
                  passengers=2, currency="GBP", market="uk")
    kwargs.update(overrides)
    return normal_transport.fetch_normal_response(**kwargs)


def test_transport_changes_only_requested_query_variables(monkeypatch):
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=b'{"data": {}}', stderr=b""))
    monkeypatch.setattr(normal_transport.subprocess, "run", run)
    assert fetch() == {"data": {}}
    args, kwargs = run.call_args
    assert args[0][0] == "powershell.exe"
    payload = json.loads(kwargs["input"])
    assert payload["operationName"] == "NewBookingSearch"
    assert payload["variables"]["adult"] == 2
    assert payload["variables"]["outbound"] == "2026-10-28"
    assert payload["variables"]["origin"] == "8814001"
    assert payload["variables"]["destination"] == "7015400"
    assert payload["variables"]["currency"] == "GBP"
    assert payload["variables"]["hasInbound"] is False
    assert kwargs["timeout"] == 45


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout=b"", stderr=b"HTTP 403"),
    SimpleNamespace(returncode=0, stdout=b"<html>Error</html>", stderr=b""),
    SimpleNamespace(returncode=0, stdout=b"[]", stderr=b""),
])
def test_failed_transport_never_returns_empty_success(monkeypatch, result):
    monkeypatch.setattr(normal_transport.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(NormalTransportError):
        fetch()


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("powershell.exe", 45)])
def test_transport_runtime_errors_are_explicit(monkeypatch, error):
    monkeypatch.setattr(normal_transport.subprocess, "run", MagicMock(side_effect=error))
    with pytest.raises(NormalTransportError):
        fetch()


@pytest.mark.parametrize("overrides", [{"origin_uic": "Brussels"}, {"passengers": 0},
                                       {"market": "uk;exit"}, {"currency": "USD"}])
def test_invalid_transport_inputs_cannot_launch_process(monkeypatch, overrides):
    run = MagicMock()
    monkeypatch.setattr(normal_transport.subprocess, "run", run)
    with pytest.raises(NormalTransportError):
        fetch(**overrides)
    run.assert_not_called()
