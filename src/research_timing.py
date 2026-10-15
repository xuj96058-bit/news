"""Shared wall-clock limits with protected time for writing and review."""
from __future__ import annotations

import math
import time


STAGE_SECONDS = {
    "designing": 180,
    "organizing": 480,
    "calculating": 180,
    "planning": 360,
    "writing": 720,
    "reviewing": 900,
    "revising": 900,
    "auditing": 720,
}


def publication_reserves(total_seconds, writing_style="argumentative"):
    """Protect the publishing stages without consuming all short budgets.

    Preparation receives at least one quarter of the overall budget. Any time
    it does not use remains available to subsequent stages; these are reserves,
    not independent timers that reset or extend the caller's deadline.
    """
    if (isinstance(total_seconds, bool) or not isinstance(total_seconds, (int, float))
            or not math.isfinite(total_seconds) or total_seconds <= 0):
        raise ValueError("Research duration must be positive and finite.")
    stages = ["writing", "reviewing", "auditing"]
    if writing_style == "argumentative":
        stages.insert(0, "planning")
    scale = min(1.0, total_seconds * 0.75 / sum(STAGE_SECONDS[stage] for stage in stages))
    return {stage: STAGE_SECONDS[stage] * scale for stage in stages}


def stage_deadline(stage, overall_deadline, *, now=None, reserve_seconds=0, phase_deadline=None):
    """Return an absolute deadline; never extend the caller's overall limit."""
    if stage not in STAGE_SECONDS:
        raise ValueError("Unknown research stage.")
    now = time.monotonic() if now is None else now
    limit = overall_deadline if phase_deadline is None else min(overall_deadline, phase_deadline)
    return min(now + STAGE_SECONDS[stage], limit - max(0, reserve_seconds))
