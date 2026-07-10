from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import FareHit


class AlertState:
    def __init__(self, path: str | Path = "data/notified.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._seen = set(json.loads(self.path.read_text()).get("seen", []))
        else:
            self._seen = set()

    def unseen(self, hits: Iterable[FareHit]) -> list[FareHit]:
        return [h for h in hits if h.dedupe_key not in self._seen]

    def mark_seen(self, hits: Iterable[FareHit]) -> None:
        for h in hits:
            self._seen.add(h.dedupe_key)
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps({"seen": sorted(self._seen)}, indent=2) + "\n")
