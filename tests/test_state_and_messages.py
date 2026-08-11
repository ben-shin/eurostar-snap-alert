from datetime import date

from eurostar_alerts.models import FareHit, Provider
from eurostar_alerts.notifier import format_hit
from eurostar_alerts.state import AlertState


def sample_hit() -> FareHit:
    return FareHit(
        provider=Provider.SNAP,
        route_name="Brussels to London",
        origin="Brussels Midi",
        destination="London St Pancras",
        travel_date=date(2026, 8, 18),
        passengers=1,
        price_amount=55,
        currency="GBP",
        booking_url="https://snap.eurostar.com/uk-en/search?outbound=2026-08-18",
        summary="Exact-date Snap availability found.",
    )


def test_whatsapp_message_uses_correct_unicode() -> None:
    message = format_hit(sample_hit())

    assert "🚄 Eurostar Snap alert" in message
    assert "Brussels Midi → London St Pancras" in message
    assert "£55" in message


def test_state_round_trip_deduplicates_hits(tmp_path) -> None:
    path = tmp_path / "notified.json"
    hit = sample_hit()

    state = AlertState(path)
    assert state.unseen([hit]) == [hit]
    state.mark_seen([hit])

    restored = AlertState(path)
    assert restored.unseen([hit]) == []
    assert not path.with_suffix(".json.tmp").exists()
