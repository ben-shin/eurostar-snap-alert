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
            self._pending = dict(raw.get("pending", {}))
        else:
            self._seen = set()
            self._pending = {}

    def unseen(self, hits: Iterable[FareHit]) -> list[FareHit]:
        return [h for h in hits if h.dedupe_key not in self._seen]

    def mark_seen(self, hits: Iterable[FareHit]) -> None:
        for h in hits:
            self._seen.add(h.dedupe_key)
            self._pending.pop(h.dedupe_key, None)
        self.save()

    def pending_sid(self, hit: FareHit):
        return self._pending.get(hit.dedupe_key)

    def record_pending(self, hit: FareHit, sid: str) -> None:
        self._pending[hit.dedupe_key] = sid
        self.save()

    def clear_pending(self, hit: FareHit) -> None:
        self._pending.pop(hit.dedupe_key, None)
        self.save()

    def restore_failed(self, hits: Iterable[FareHit]) -> int:
        keys = {hit.dedupe_key for hit in hits} & self._seen
        if keys:
            self._seen.difference_update(keys)
            self.save()
        return len(keys)

    def save(self) -> None:
        payload = json.dumps({"seen": sorted(self._seen), "pending": self._pending}, indent=2) + "\n"
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)
