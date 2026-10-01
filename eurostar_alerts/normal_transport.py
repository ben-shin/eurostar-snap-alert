"""Read Eurostar's public NewBookingSearch response with the proven Windows HTTP client."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import re
import subprocess
from typing import Any


class NormalTransportError(RuntimeError):
    """The public fare request did not produce a valid response."""


def fetch_normal_response(
    *,
    origin_uic: str,
    destination_uic: str,
    travel_date: date,
    passengers: int,
    currency: str,
    market: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"\d{7}", origin_uic) or not re.fullmatch(r"\d{7}", destination_uic):
        raise NormalTransportError("Selected stations did not provide valid UIC codes")
    if not 1 <= passengers <= 4 or currency not in {"GBP", "EUR"} or not re.fullmatch(r"[a-z]{2}", market):
        raise NormalTransportError("Invalid normal fare request parameters")

    template = Path(__file__).with_name("normal_search_query.json")
    payload = json.loads(template.read_text(encoding="utf-8"))
    payload["variables"].update(
        origin=origin_uic,
        destination=destination_uic,
        outbound=travel_date.isoformat(),
        adult=passengers,
        currency=currency,
    )
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(Path(__file__).with_name("normal_search.ps1")), "-Market", market,
    ]
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            capture_output=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NormalTransportError(f"Eurostar fare transport failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise NormalTransportError(f"Eurostar fare request failed: {detail or 'PowerShell exited unsuccessfully'}")
    try:
        response = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NormalTransportError("Eurostar fare request returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise NormalTransportError("Eurostar fare request returned invalid JSON")
    return response

