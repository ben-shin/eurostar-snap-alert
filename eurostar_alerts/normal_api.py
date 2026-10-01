"""Pure parsing of Eurostar's public NewBookingSearch response.

The response does not normally echo currency or passenger count. Callers must
supply those from the exact request that produced the response. Prices are the
whole-party ``prices.total`` in currency units, not cents or per-person prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any


class NormalApiError(ValueError):
    """The response cannot establish availability for the requested journey."""


@dataclass(frozen=True)
class NormalFare:
    price_amount: float
    currency: str
    departure_time: str | None
    arrival_time: str | None
    duration: str | None
    fare_class: str | None


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NormalApiError(f"Missing or malformed {label}")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise NormalApiError(f"Missing or malformed {label}")
    return value


def _currency(value: dict[str, Any], expected: str) -> None:
    # Most responses omit this field; do not manufacture response confirmation.
    if "currency" in value and value["currency"] != expected:
        raise NormalApiError("Response currency does not match request currency")


def _capacity(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NormalApiError(f"Malformed {label}")
    return value


def _time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise NormalApiError("Malformed journey time")
    return value


def _duration(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise NormalApiError("Malformed journey duration")
    hours, minutes = divmod(value, 60)
    return f"{hours}h {minutes:02d}m"


def parse_normal_response(
    payload: Any,
    *,
    origin_uic: str,
    destination_uic: str,
    travel_date: date,
    currency: str,
    passengers: int = 1,
) -> NormalFare | None:
    """Return the cheapest bookable base fare, or None for valid no fares.

    GraphQL errors, malformed required data and mismatched route/date/currency
    raise NormalApiError; they must not be counted as a successful empty scan.
    Times remain station-local, and duration uses the supplied minutes rather
    than subtracting departure and arrival across different time zones.
    Equal prices retain the first journey and its own timing information.
    """
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise NormalApiError("Request currency must be a three-letter uppercase code")
    if isinstance(passengers, bool) or not isinstance(passengers, int) or passengers < 1:
        raise NormalApiError("Request passengers must be a positive integer")
    document = _object(payload, "response")
    if document.get("errors"):
        raise NormalApiError("Eurostar returned GraphQL errors")
    data = _object(document.get("data"), "response data")
    search = _object(data.get("journeySearch"), "journey search")
    outbound = _object(search.get("outbound"), "outbound results")
    for container in (document, data, search, outbound):
        _currency(container, currency)
    origin = _object(outbound.get("origin"), "outbound origin")
    destination = _object(outbound.get("destination"), "outbound destination")
    if origin.get("uic") != origin_uic or destination.get("uic") != destination_uic:
        raise NormalApiError("Response route does not match requested route")

    cheapest: NormalFare | None = None
    for raw_journey in _array(outbound.get("journeys"), "outbound journeys"):
        journey = _object(raw_journey, "journey")
        _currency(journey, currency)
        timing = _object(journey.get("timing"), "journey timing")
        if timing.get("date") != travel_date.isoformat():
            raise NormalApiError("Response date does not match requested date")
        departure = _time(timing.get("departureTime"))
        arrival = _time(timing.get("arrivalTime"))
        duration = _duration(timing.get("duration"))
        for raw_fare in _array(journey.get("fares"), "journey fares"):
            # Null fare entries and null totals represent no offered fare.
            if raw_fare is None:
                continue
            fare = _object(raw_fare, "fare")
            cabin = _object(fare.get("classOfService"), "fare class of service")
            class_name = cabin.get("name")
            if not isinstance(class_name, str) or not class_name.strip():
                raise NormalApiError("Malformed fare class")
            _currency(fare, currency)
            prices = _object(fare.get("prices"), "fare prices")
            _currency(prices, currency)
            if "total" not in prices:
                raise NormalApiError("Missing fare total")
            amount = prices["total"]
            if amount is None:
                continue
            if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                    or not math.isfinite(amount) or amount <= 0):
                raise NormalApiError("Malformed fare total")
            if "seats" not in fare or "availabilityOfClassOfService" not in fare:
                raise NormalApiError("Missing fare availability")
            seats = _capacity(fare["seats"], "fare seats")
            availability = _capacity(fare["availabilityOfClassOfService"], "class availability")
            if seats is None or availability is None or min(seats, availability) < passengers:
                continue
            candidate = NormalFare(float(amount), currency, departure, arrival, duration, class_name.strip())
            if cheapest is None or candidate.price_amount < cheapest.price_amount:
                cheapest = candidate
    return cheapest
