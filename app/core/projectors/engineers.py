"""Engineer stage, grade and grade progress (DESIGN §9.1, §9.2).

`EngineerProgress` at login lists only discovered engineers. Live updates carry
one engineer at the top level, sometimes with only `Rank` or only `Progress`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.projectors.base import Writer

EVENTS = ("EngineerProgress",)

# Stored as numbers in snapshots; 0 is a stage string we don't know yet.
STAGES = {"Known": 1, "Invited": 2, "Acquainted": 3, "Unlocked": 4, "Barred": -1}
STAGE_NAMES = {v: k for k, v in STAGES.items()} | {0: "Unknown"}


@dataclass
class State:
    stage: dict[str, int] = field(default_factory=dict)
    grade: dict[str, int] = field(default_factory=dict)


def _one(state: State, w: Writer, entry: dict) -> None:
    eid = entry.get("EngineerID")
    if eid is None:
        return
    key = str(eid)
    if isinstance(entry.get("Progress"), str):
        stage = STAGES.get(entry["Progress"], 0)
        previous = state.stage.get(key)
        if previous is not None and stage != previous:
            w.unlock("engineer_stage", key, **{"from": STAGE_NAMES[previous], "to": STAGE_NAMES[stage]})
        state.stage[key] = stage
        w.snapshot("engineer_stage", key, stage)
    if isinstance(entry.get("Rank"), int):
        grade = entry["Rank"]
        previous = state.grade.get(key)
        if previous is not None and grade > previous:
            w.unlock("engineer_grade", key, **{"from": previous, "to": grade})
        state.grade[key] = grade
        w.snapshot("engineer_grade", key, grade)
        if state.stage.get(key) != STAGES["Unlocked"]:
            # A grade only exists for unlocked engineers; live updates may omit Progress.
            state.stage[key] = STAGES["Unlocked"]
            w.snapshot("engineer_stage", key, STAGES["Unlocked"])
    if isinstance(entry.get("RankProgress"), int):
        w.snapshot("engineer_progress", key, entry["RankProgress"])


def handle(state: State, w: Writer, name: str, event: dict) -> None:
    if isinstance(event.get("Engineers"), list):
        for entry in event["Engineers"]:
            _one(state, w, entry)
    else:
        _one(state, w, event)
