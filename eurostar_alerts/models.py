from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class Provider(str, Enum):
    SNAP = "snap"
    NORMAL = "normal_eurostar"


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
