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
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("seen", []), list):
                raise ValueError(f"Invalid alert state file: {self.path}")
            self._seen = {str(value) for value in raw.get("seen", [])}
        else:
            self._seen = set()

    def unseen(self, hits: Iterable[FareHit]) -> list[FareHit]:
        return [h for h in hits if h.dedupe_key not in self._seen]

    def mark_seen(self, hits: Iterable[FareHit]) -> None:
        for h in hits:
            self._seen.add(h.dedupe_key)
        self.save()

    def save(self) -> None:
        payload = json.dumps({"seen": sorted(self._seen)}, indent=2) + "\n"
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
