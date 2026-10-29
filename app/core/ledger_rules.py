"""`refdata/ledger_map.csv`: journal event → ledger rows (DESIGN §9.3). Stdlib only.

Each row: event, condition (`Field=value` or blank), currency (cr|mc), category,
sign (+|-), amount, item. `amount` is a field name, a sum of fields (`A+B`), or a
sum over a list (`sum(List.Field)` or `sum(List.A+List.B)`). Rows that share an
event and amount form a group: the first whose condition matches wins, so
`MarketSell` with `BlackMarket=true` goes to smuggling and the rest to trade.
A row whose amount field is absent or zero writes nothing.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COLUMNS = ["event", "condition", "currency", "category", "sign", "amount", "item"]
CURRENCIES = {"cr", "mc"}
_SUM = re.compile(r"^sum\((.+)\)$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ALWAYS = object()


@dataclass(frozen=True)
class Rule:
    event: str
    field: str | None
    value: Any
    currency: str
    category: str
    sign: int
    amount: str
    item: str | None

    def matches(self, event: dict) -> bool:
        return self.value is _ALWAYS or (self.field in event and event[self.field] == self.value)


@dataclass(frozen=True)
class Entry:
    currency: str
    category: str
    amount: int
    item: str | None


def parse_amount(expr: str) -> tuple[str | None, list[str]]:
    """`(list_field or None, [fields])`; raises ValueError if malformed."""
    expr = expr.strip()
    m = _SUM.match(expr)
    if m:
        parts = [p.strip() for p in m.group(1).split("+")]
        lists = {p.split(".", 1)[0] for p in parts}
        if len(lists) != 1 or any("." not in p for p in parts):
            raise ValueError(f"sum() must read one list: {expr!r}")
        fields = [p.split(".", 1)[1] for p in parts]
        if not all(_NAME.match(f) for f in fields):
            raise ValueError(f"bad field in {expr!r}")
        return lists.pop(), fields
    fields = [p.strip() for p in expr.split("+")]
    if not fields or not all(_NAME.match(f) for f in fields):
        raise ValueError(f"bad amount {expr!r}")
    return None, fields


def _number(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def evaluate(expr: str, event: dict) -> float:
    list_field, fields = parse_amount(expr)
    if list_field is None:
        return sum(_number(event.get(f)) for f in fields)
    return sum(_number(entry.get(f)) for entry in event.get(list_field) or [] if isinstance(entry, dict)
               for f in fields)


def _parse_condition(text: str) -> tuple[str | None, Any]:
    text = (text or "").strip()
    if not text:
        return None, _ALWAYS
    field, sep, value = text.partition("=")
    if not sep:
        raise ValueError(f"bad condition {text!r}")
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed = value.strip()
    return field.strip(), parsed


class LedgerMap:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._groups: dict[str, list[list[Rule]]] = {}
        for rule in rules:
            groups = self._groups.setdefault(rule.event, [])
            for group in groups:
                if group[0].amount == rule.amount:
                    group.append(rule)
                    break
            else:
                groups.append([rule])

    @classmethod
    def from_csv(cls, path: Path) -> "LedgerMap":
        rules = []
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                field, value = _parse_condition(row["condition"])
                parse_amount(row["amount"])
                rules.append(Rule(
                    event=row["event"].strip(), field=field, value=value, currency=row["currency"].strip(),
                    category=row["category"].strip(), sign=-1 if row["sign"].strip() == "-" else 1,
                    amount=row["amount"].strip(), item=(row.get("item") or "").strip() or None,
                ))
        return cls(rules)

    @property
    def events(self) -> set[str]:
        return set(self._groups)

    def entries(self, name: str, event: dict) -> list[Entry]:
        out = []
        for group in self._groups.get(name, ()):
            rule = next((r for r in group if r.matches(event)), None)
            if rule is None:
                continue
            amount = round(evaluate(rule.amount, event))
            if not amount:
                continue
            item = event.get(rule.item) if rule.item else None
            out.append(Entry(rule.currency, rule.category, rule.sign * amount,
                             item if isinstance(item, str) else None))
        return out
