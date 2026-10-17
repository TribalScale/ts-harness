from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class MetricsRegistry:
    counters: dict[str, float] = field(default_factory=dict)
    observations: dict[str, list[float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            bucket = self.observations.setdefault(name, [])
            bucket.append(value)
            if len(bucket) > 100:
                del bucket[:-100]

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            result = dict(self.counters)
            for name, values in self.observations.items():
                if values:
                    result[f"{name}_last"] = values[-1]
                    result[f"{name}_avg"] = sum(values) / len(values)
            return result
