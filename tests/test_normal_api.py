from datetime import date
import json
from pathlib import Path

import pytest

from eurostar_alerts.normal_api import NormalApiError, parse_normal_response


@pytest.fixture
def response():
    # Sanitized first two trains from a successful live NewBookingSearch response.
    path = Path(__file__).parent / "fixtures" / "normal_new_booking_search.json"
    return json.loads(path.read_text())


def parse(response, **overrides):
    arguments = dict(
        origin_uic="8814001", destination_uic="7015400",
        travel_date=date(2026, 10, 28), currency="GBP", passengers=1,
    )
    arguments.update(overrides)
    return parse_normal_response(response, **arguments)


def journeys(response):
    return response["data"]["journeySearch"]["outbound"]["journeys"]


def standard(journey):
    return next(f for f in journey["fares"] if f["classOfService"]["code"] == "STANDARD")


def test_actual_response_keeps_currency_units_and_station_local_times(response):
    fare = parse(response)
    assert fare.price_amount == 105.0
    assert fare.currency == "GBP"
    assert (fare.departure_time, fare.arrival_time, fare.duration) == ("07:56", "08:57", "2h 01m")


def test_cheapest_fare_keeps_its_own_journey_times(response):
    standard(journeys(response)[1])["prices"]["total"] = 80.5
    fare = parse(response)
    assert fare.price_amount == 80.5
    assert (fare.departure_time, fare.arrival_time, fare.duration) == ("08:52", "09:57", "2h 05m")


def test_equal_fares_keep_one_complete_journey(response):
    standard(journeys(response)[1])["prices"]["total"] = 105
    fare = parse(response)
    assert (fare.departure_time, fare.arrival_time, fare.duration) == ("07:56", "08:57", "2h 01m")


def test_cheaper_plus_base_fare_can_win_with_its_class_and_times(response):
    plus = journeys(response)[0]["fares"][1]
    plus["prices"]["total"] = 90
    fare = parse(response)
    assert fare.price_amount == 90
    assert fare.fare_class == plus["classOfService"]["name"]
    assert (fare.departure_time, fare.arrival_time) == ("07:56", "08:57")


def test_whole_party_total_is_not_per_person_display_price(response):
    first = standard(journeys(response)[0])
    first["prices"] = {"total": 120, "displayPrice": 60}
    fare = parse(response, passengers=2)
    assert fare.price_amount == 120


@pytest.mark.parametrize("field,value", [
    ("seats", 0), ("seats", None),
    ("availabilityOfClassOfService", 0), ("availabilityOfClassOfService", None),
])
def test_sold_out_or_unoffered_fare_does_not_win(response, field, value):
    first = standard(journeys(response)[0])
    first["prices"]["total"] = 20
    first[field] = value
    fare = parse(response)
    assert fare.price_amount == 130.5
    assert fare.fare_class == "Eurostar Plus"
    assert fare.departure_time == "07:56"


def test_fare_needs_seats_for_every_requested_passenger(response):
    for fare in journeys(response)[0]["fares"]:
        fare["seats"] = 1
    assert parse(response, passengers=2).departure_time == "08:52"


def test_null_price_and_addons_cannot_win(response):
    standard(journeys(response)[0])["prices"]["total"] = None
    journeys(response)[0]["fares"][1]["prices"]["total"] = 150
    journeys(response)[0]["fares"].append(None)
    journeys(response)[1]["fares"][0]["legs"] = [{"addOnProducts": [{"price": 1}]}]
    assert parse(response).price_amount == 135.5


@pytest.mark.parametrize("empty_kind", ["no_journeys", "no_fares", "sold_out"])
def test_valid_empty_results_return_none(response, empty_kind):
    if empty_kind == "no_journeys":
        journeys(response).clear()
    else:
        for journey in journeys(response):
            if empty_kind == "no_fares":
                journey["fares"].clear()
            elif empty_kind == "sold_out":
                for fare in journey["fares"]:
                    fare["seats"] = 0
    assert parse(response) is None


@pytest.mark.parametrize("field,value", [("origin", "7015400"), ("destination", "8727100")])
def test_wrong_route_is_failure_even_when_fares_exist(response, field, value):
    response["data"]["journeySearch"]["outbound"][field]["uic"] = value
    with pytest.raises(NormalApiError, match="route"):
        parse(response)


def test_adjacent_date_is_failure_not_a_cheaper_fare(response):
    journeys(response)[1]["timing"]["date"] = "2026-10-29"
    standard(journeys(response)[1])["prices"]["total"] = 5
    with pytest.raises(NormalApiError, match="date"):
        parse(response)


@pytest.mark.parametrize("location", ["response", "outbound", "fare", "prices"])
def test_explicit_wrong_currency_is_rejected(response, location):
    containers = {
        "response": response,
        "outbound": response["data"]["journeySearch"]["outbound"],
        "fare": standard(journeys(response)[0]),
        "prices": standard(journeys(response)[0])["prices"],
    }
    containers[location]["currency"] = "EUR"
    with pytest.raises(NormalApiError, match="currency"):
        parse(response)


def test_currency_is_from_request_when_response_does_not_echo_it(response):
    assert parse(response, currency="EUR").currency == "EUR"
    # The parser cannot independently verify an unechoed currency; caller must
    # supply the exact query currency, rather than an alert currency preference.


def test_graphql_partial_data_with_errors_is_failure(response):
    response["errors"] = [{"message": "service unavailable"}]
    with pytest.raises(NormalApiError, match="GraphQL"):
        parse(response)


@pytest.mark.parametrize("payload", [None, [], {}, {"message": "Forbidden"}, {"data": None},
                                        {"data": {"journeySearch": {"outbound": None}}}])
def test_malformed_or_error_response_is_not_an_empty_success(payload):
    with pytest.raises(NormalApiError):
        parse(payload)


@pytest.mark.parametrize("field", ["prices", "seats", "availabilityOfClassOfService"])
def test_missing_standard_fare_fields_are_failure(response, field):
    del standard(journeys(response)[0])[field]
    with pytest.raises(NormalApiError):
        parse(response)


@pytest.mark.parametrize("amount", [-1, 0, True, "105", float("nan"), float("inf")])
def test_malformed_total_is_not_a_valid_fare(response, amount):
    standard(journeys(response)[0])["prices"]["total"] = amount
    with pytest.raises(NormalApiError, match="total"):
        parse(response)


def test_missing_optional_times_are_not_invented(response):
    timing = journeys(response)[0]["timing"]
    for field in ("departureTime", "arrivalTime", "duration"):
        timing.pop(field)
    fare = parse(response)
    assert (fare.departure_time, fare.arrival_time, fare.duration) == (None, None, None)


@pytest.mark.parametrize("field,value", [("departureTime", "25:12"), ("arrivalTime", "09:90"),
                                          ("duration", -1), ("duration", "121")])
def test_malformed_optional_times_do_not_produce_misleading_details(response, field, value):
    journeys(response)[0]["timing"][field] = value
    with pytest.raises(NormalApiError):
        parse(response)
