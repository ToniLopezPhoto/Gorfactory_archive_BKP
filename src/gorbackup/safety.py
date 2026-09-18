"""Plan-based safety gates applied immediately before backup execution."""

import shutil
from dataclasses import dataclass
from typing import Callable, Sequence

from gorbackup.config import AppConfig
from gorbackup.planner import PlanResult

GIB = 1024 ** 3


class SafetyError(RuntimeError):
    """Raised when a plan exceeds one or more configured safety limits."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class SafetyAssessment:
    delete_count: int
    delete_bytes: int
    transfer_bytes: int
    free_bytes_before: int
    free_bytes_after: int
    free_percent_after: float


def assess_plan_safety(
    config: AppConfig,
    plan: PlanResult,
    *,
    disk_usage: Callable[[object], object] = shutil.disk_usage,
) -> SafetyAssessment:
    """Reject a plan that exceeds delete or projected-capacity limits."""
    destructive = tuple(
        item
        for item in plan.items
        if item.category in {"delete_from_current", "rename_move_candidate"}
    )
    delete_count = len(destructive)
    delete_bytes = sum(item.leaving_size for item in destructive)
    usage = disk_usage(config.archive.root)
    total = int(usage.total)
    free_before = int(usage.free)
    free_after = free_before - plan.transfer_bytes
    free_percent_after = (free_after / total * 100) if total else 0.0

    reasons = []
    if delete_count > config.safety.max_deletes_per_run:
        reasons.append(
            "planned deletions exceed safety.max_deletes_per_run "
            f"({delete_count} > {config.safety.max_deletes_per_run})"
        )
    max_delete_bytes = int(config.safety.max_delete_size_gb * GIB)
    if delete_bytes > max_delete_bytes:
        reasons.append(
            "planned deletion size exceeds safety.max_delete_size_gb "
            f"({delete_bytes} bytes > {max_delete_bytes} bytes)"
        )
    if free_after < 0:
        reasons.append(
            "insufficient archive space for planned transfers "
            f"({plan.transfer_bytes} required, {free_before} available)"
        )
    elif free_percent_after < config.safety.min_free_space_percent:
        reasons.append(
            "projected archive free space is below safety.min_free_space_percent "
            f"({free_percent_after:.1f}% < "
            f"{config.safety.min_free_space_percent:.1f}%)"
        )
    if reasons:
        raise SafetyError(reasons)

    return SafetyAssessment(
        delete_count,
        delete_bytes,
        plan.transfer_bytes,
        free_before,
        free_after,
        free_percent_after,
    )
