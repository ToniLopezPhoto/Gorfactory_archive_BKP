"""Plan-based safety gates applied immediately before backup execution."""

import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Tuple

from gorbackup.config import AppConfig
from gorbackup.planner import PlanResult

GIB = 1024 ** 3
OVERRIDABLE_GATES = frozenset({
    "delete_count", "delete_bytes", "catalogue_file_count_shrink",
    "catalogue_bytes_shrink", "high_change_count", "high_change_bytes",
    "high_change_ratio",
})


@dataclass(frozen=True)
class SafetyReference:
    kind: str
    run_id: Optional[str]
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class GateFailure:
    gate: str
    measured: float
    threshold: float
    unit: str
    message: str
    overridable: bool


@dataclass(frozen=True)
class SafetyAssessment:
    delete_count: int
    delete_bytes: int
    transfer_bytes: int
    free_bytes_before: int
    free_bytes_after: int
    free_percent_after: float
    change_count: int = 0
    change_bytes: int = 0
    change_catalogue_percent: float = 0.0
    source_file_count: int = 0
    source_total_bytes: int = 0
    reference_kind: str = "none"
    reference_run_id: Optional[str] = None
    reference_file_count: int = 0
    reference_total_bytes: int = 0
    file_count_delta: int = 0
    file_count_change_percent: float = 0.0
    total_bytes_delta: int = 0
    total_bytes_change_percent: float = 0.0
    failures: Tuple[GateFailure, ...] = ()
    thresholds: Dict[str, float] = field(default_factory=dict)


class SafetyError(RuntimeError):
    """Raised when a plan exceeds one or more configured safety limits."""

    def __init__(self, reasons: Sequence[str], assessment: Optional[SafetyAssessment] = None) -> None:
        self.reasons = tuple(reasons)
        self.assessment = assessment
        super().__init__("; ".join(self.reasons))


def _percent_delta(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return (current - previous) / previous * 100


def assess_plan_safety(
    config: AppConfig,
    plan: PlanResult,
    *,
    reference: Optional[SafetyReference] = None,
    disk_usage: Callable[[object], object] = shutil.disk_usage,
) -> SafetyAssessment:
    """Assess deletion, shrink, change-volume, and capacity gates."""
    destructive = tuple(item for item in plan.items if item.category in {
        "delete_from_current", "rename_move_candidate"
    })
    changed = tuple(item for item in plan.items if item.category in {
        "new_file", "changed_file", "rename_move_candidate"
    })
    delete_count = len(destructive)
    delete_bytes = sum(item.leaving_size for item in destructive)
    change_count = len(changed)
    change_bytes = sum(item.size for item in changed)
    catalogue_denominator = reference.file_count if reference and reference.file_count else plan.catalogue_file_count
    change_ratio = change_count / catalogue_denominator * 100 if catalogue_denominator else 0.0
    usage = disk_usage(config.archive.root)
    total = int(usage.total)
    free_before = int(usage.free)
    free_after = free_before - plan.transfer_bytes
    free_percent_after = (free_after / total * 100) if total else 0.0

    reference = reference or SafetyReference("none", None, 0, 0)
    file_delta = plan.catalogue_file_count - reference.file_count
    byte_delta = plan.catalogue_total_bytes - reference.total_bytes
    file_percent = _percent_delta(plan.catalogue_file_count, reference.file_count)
    byte_percent = _percent_delta(plan.catalogue_total_bytes, reference.total_bytes)
    failures = []

    def fail(gate: str, measured: float, threshold: float, unit: str,
             message: str, *, overridable: bool = True) -> None:
        failures.append(GateFailure(gate, measured, threshold, unit, message, overridable))

    if delete_count > config.safety.max_deletes_per_run:
        fail("delete_count", delete_count, config.safety.max_deletes_per_run, "files",
             f"planned deletions exceed safety.max_deletes_per_run ({delete_count} > {config.safety.max_deletes_per_run})")
    max_delete_bytes = int(config.safety.max_delete_size_gb * GIB)
    if delete_bytes > max_delete_bytes:
        fail("delete_bytes", delete_bytes, max_delete_bytes, "bytes",
             f"planned deletion size exceeds safety.max_delete_size_gb ({delete_bytes} bytes > {max_delete_bytes} bytes)")

    if reference.kind != "none":
        file_drop, byte_drop = max(0, -file_delta), max(0, -byte_delta)
        file_drop_percent, byte_drop_percent = max(0.0, -file_percent), max(0.0, -byte_percent)
        file_absolute_failed = file_drop > config.safety.max_source_file_count_drop
        if file_absolute_failed or file_drop_percent > config.safety.max_source_file_count_drop_percent:
            fail("catalogue_file_count_shrink",
                 file_drop if file_absolute_failed else file_drop_percent,
                 config.safety.max_source_file_count_drop if file_absolute_failed else config.safety.max_source_file_count_drop_percent,
                 "files" if file_absolute_failed else "percent",
                 f"source file count shrank against {reference.kind}: {file_drop} files ({file_drop_percent:.2f}%); limits are {config.safety.max_source_file_count_drop} files or {config.safety.max_source_file_count_drop_percent:.2f}%")
        max_byte_drop = int(config.safety.max_source_bytes_drop_gb * GIB)
        byte_absolute_failed = byte_drop > max_byte_drop
        if byte_absolute_failed or byte_drop_percent > config.safety.max_source_bytes_drop_percent:
            fail("catalogue_bytes_shrink",
                 byte_drop if byte_absolute_failed else byte_drop_percent,
                 max_byte_drop if byte_absolute_failed else config.safety.max_source_bytes_drop_percent,
                 "bytes" if byte_absolute_failed else "percent",
                 f"source bytes shrank against {reference.kind}: {byte_drop} bytes ({byte_drop_percent:.2f}%); limits are {max_byte_drop} bytes or {config.safety.max_source_bytes_drop_percent:.2f}%")

    if change_count > config.safety.max_changed_files_per_run:
        fail("high_change_count", change_count, config.safety.max_changed_files_per_run, "files",
             f"planned changed files exceed safety.max_changed_files_per_run ({change_count} > {config.safety.max_changed_files_per_run})")
    max_change_bytes = int(config.safety.max_changed_bytes_gb * GIB)
    if change_bytes > max_change_bytes:
        fail("high_change_bytes", change_bytes, max_change_bytes, "bytes",
             f"planned changed bytes exceed safety.max_changed_bytes_gb ({change_bytes} > {max_change_bytes})")
    if change_ratio > config.safety.max_changed_catalogue_percent:
        fail("high_change_ratio", change_ratio, config.safety.max_changed_catalogue_percent, "percent",
             f"planned changed catalogue ratio exceeds safety.max_changed_catalogue_percent ({change_ratio:.2f}% > {config.safety.max_changed_catalogue_percent:.2f}%)")
    if free_after < 0:
        fail("archive_capacity", plan.transfer_bytes, free_before, "bytes",
             f"insufficient archive space for planned transfers ({plan.transfer_bytes} required, {free_before} available)", overridable=False)
    elif free_percent_after < config.safety.min_free_space_percent:
        fail("archive_free_percent", free_percent_after, config.safety.min_free_space_percent, "percent",
             f"projected archive free space is below safety.min_free_space_percent ({free_percent_after:.1f}% < {config.safety.min_free_space_percent:.1f}%)", overridable=False)

    assessment = SafetyAssessment(
        delete_count=delete_count, delete_bytes=delete_bytes,
        transfer_bytes=plan.transfer_bytes, free_bytes_before=free_before,
        free_bytes_after=free_after, free_percent_after=free_percent_after,
        change_count=change_count, change_bytes=change_bytes,
        change_catalogue_percent=change_ratio,
        source_file_count=plan.catalogue_file_count,
        source_total_bytes=plan.catalogue_total_bytes,
        reference_kind=reference.kind, reference_run_id=reference.run_id,
        reference_file_count=reference.file_count,
        reference_total_bytes=reference.total_bytes,
        file_count_delta=file_delta, file_count_change_percent=file_percent,
        total_bytes_delta=byte_delta, total_bytes_change_percent=byte_percent,
        failures=tuple(failures),
        thresholds={
            "max_deletes_per_run": config.safety.max_deletes_per_run,
            "max_delete_bytes": max_delete_bytes,
            "max_source_file_count_drop": config.safety.max_source_file_count_drop,
            "max_source_file_count_drop_percent": config.safety.max_source_file_count_drop_percent,
            "max_source_bytes_drop": int(config.safety.max_source_bytes_drop_gb * GIB),
            "max_source_bytes_drop_percent": config.safety.max_source_bytes_drop_percent,
            "max_changed_files_per_run": config.safety.max_changed_files_per_run,
            "max_changed_bytes": max_change_bytes,
            "max_changed_catalogue_percent": config.safety.max_changed_catalogue_percent,
            "min_free_space_percent": config.safety.min_free_space_percent,
        },
    )
    if failures:
        raise SafetyError([item.message for item in failures], assessment)
    return assessment
