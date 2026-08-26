from eurostar_alerts.scrapers import cheapest_allowed_price, parse_prices, station_candidates


def test_parse_prices_handles_symbols_codes_and_split_decimals() -> None:
    text = "From £55, €39.50, 115.50 GBP and £ 65 .00"

    assert parse_prices(text) == [
        ("GBP", 55.0),
        ("EUR", 39.5),
        ("GBP", 115.5),
        ("GBP", 65.0),
    ]


def test_cheapest_price_uses_only_supplied_exact_date_texts() -> None:
    exact_date_prices = ["Morning £65", "Afternoon £55"]

    assert cheapest_allowed_price(exact_date_prices, {"GBP", "EUR"}) == ("GBP", 55.0)


def test_normal_price_floor_ignores_ancillary_amounts() -> None:
    assert cheapest_allowed_price(["Fee £4.50 Fare £49"], {"GBP"}, minimum=30) == ("GBP", 49.0)


def test_station_candidates_include_live_eurostar_names() -> None:
    assert "Brussels Midi" in station_candidates("Brussels Midi/Zuid")
    assert "London St Pancras" in station_candidates("London St Pancras International")
