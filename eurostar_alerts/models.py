from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class Provider(str, Enum):
    SNAP = "snap"
    NORMAL = "normal_eurostar"


class CheckStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class RouteQuery:
    name: str
    origin: str
    destination: str
    start_date: date
    end_date: date
    passengers: int = 1
    snap_link: str = "https://snap.eurostar.com/uk-en"
    booking_link: str = "https://www.eurostar.com/rw-en"


@dataclass(frozen=True)
class FareHit:
    provider: Provider
    route_name: str
    origin: str
    destination: str
    travel_date: date
    passengers: int
    price_amount: Optional[float]
    currency: Optional[str]
    booking_url: str
    summary: str

    @property
    def dedupe_key(self) -> str:
        price = "any" if self.price_amount is None else f"{self.currency or 'UNK'}{self.price_amount:.2f}"
        return "|".join(
            [
                self.provider.value,
                self.route_name,
                self.origin,
                self.destination,
                self.travel_date.isoformat(),
                str(self.passengers),
                price,
            ]
        )


@dataclass(frozen=True)
class CheckOutcome:
    provider: Provider
    route_name: str
    travel_date: date
    status: CheckStatus
    message: str
    hit: Optional[FareHit] = None


@dataclass
class ScrapeReport:
    outcomes: list[CheckOutcome]

    @property
    def hits(self) -> list[FareHit]:
        return [outcome.hit for outcome in self.outcomes if outcome.hit is not None]

    @property
    def failures(self) -> list[CheckOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status == CheckStatus.FAILED]

    @property
    def attempted(self) -> int:
        return len(self.outcomes)

    @property
    def completed(self) -> int:
        return self.attempted - len(self.failures)

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "completed": self.completed,
            "failed": len(self.failures),
            "hits": len(self.hits),
            "outcomes": [
                {
                    "provider": outcome.provider.value,
                    "route": outcome.route_name,
                    "date": outcome.travel_date.isoformat(),
                    "status": outcome.status.value,
                    "message": outcome.message,
                }
                for outcome in self.outcomes
            ],
        }
