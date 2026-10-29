"""Events dropped at ingest, from refdata/ignored_events.csv (DESIGN §5.6)."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ALWAYS = object()


@dataclass(frozen=True)
class Rule:
    event: str
    field: str | None
    value: Any
    reason: str


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _parse_condition(condition: str) -> tuple[str | None, Any]:
    condition = condition.strip()
    if not condition:
        return None, _ALWAYS
    field, sep, value = condition.partition("=")
    if not sep or not field.strip():
        raise ValueError(f"bad ignored_events condition: {condition!r}")
    return field.strip(), _parse_value(value.strip())


class IngestFilter:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._by_event: dict[str, list[Rule]] = {}
        for rule in rules:
            self._by_event.setdefault(rule.event, []).append(rule)

    @classmethod
    def from_csv(cls, path: Path) -> "IngestFilter":
        rules = []
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                field, value = _parse_condition(row.get("condition") or "")
                rules.append(Rule(row["event"].strip(), field, value, (row.get("reason") or "").strip()))
        return cls(rules)

    def drops(self, event: dict) -> bool:
        for rule in self._by_event.get(event.get("event", ""), ()):
            if rule.value is _ALWAYS:
                return True
            if rule.field in event and event[rule.field] == rule.value:
                return True
        return False
