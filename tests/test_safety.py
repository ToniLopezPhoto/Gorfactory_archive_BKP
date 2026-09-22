from collections import namedtuple
from dataclasses import replace
from pathlib import Path

import pytest

from gorbackup.config import (
    AppConfig,
    ArchiveConfig,
    LoggingConfig,
    RetentionConfig,
    SafetyConfig,
    SourceConfig,
)
from gorbackup.planner import PlanItem, PlanResult
from gorbackup.safety import GIB, SafetyError, SafetyReference, assess_plan_safety

Usage = namedtuple("Usage", "total used free")


def make_config(tmp_path: Path, safety: SafetyConfig) -> AppConfig:
    return AppConfig(
        SourceConfig(tmp_path / "source", tmp_path, ".source", "source"),
        ArchiveConfig(tmp_path / "archive", "current", "history", ".archive", "archive"),
        safety,
        RetentionConfig(False),
        LoggingConfig(),
    )


def make_plan(*items: PlanItem, transfer_bytes: int = 0) -> PlanResult:
    return PlanResult(
        "plan-1",
        "success",
        items,
        {},
        {},
        0,
        0,
        0,
        transfer_bytes,
        len([item for item in items if item.leaving_size]),
        sum(item.leaving_size for item in items),
        Path("plan.json"),
    )


def assess(config, plan, reference=None):
    return assess_plan_safety(
        config, plan, reference=reference,
        disk_usage=lambda path: Usage(1000 * GIB, 0, 900 * GIB),
    )


def test_assessment_reports_delete_and_projected_capacity(tmp_path: Path) -> None:
    config = make_config(tmp_path, SafetyConfig(2, 2, 10, 0, 0, 0.8))
    plan = make_plan(
        PlanItem("delete_from_current", "old.tif", 1, "old", leaving_size=GIB),
        PlanItem("changed_file", "edit.tif", GIB, "changed", leaving_size=GIB),
        transfer_bytes=GIB,
    )

    result = assess_plan_safety(
        config,
        plan,
        disk_usage=lambda path: Usage(10 * GIB, 5 * GIB, 5 * GIB),
    )

    assert result.delete_count == 1
    assert result.delete_bytes == GIB
    assert result.free_bytes_after == 4 * GIB
    assert result.free_percent_after == 40.0


def test_rename_candidate_counts_as_destination_delete(tmp_path: Path) -> None:
    config = make_config(tmp_path, SafetyConfig(0, 2, 0, 0, 0, 0.8))
    plan = make_plan(
        PlanItem(
            "rename_move_candidate",
            "new.tif",
            10,
            "rename",
            related_path="old.tif",
            leaving_size=10,
        )
    )

    with pytest.raises(SafetyError, match="max_deletes_per_run"):
        assess_plan_safety(
            config,
            plan,
            disk_usage=lambda path: Usage(100, 0, 100),
        )


def test_all_safety_failures_are_reported_together(tmp_path: Path) -> None:
    config = make_config(tmp_path, SafetyConfig(0, 0, 50, 0, 0, 0.8))
    plan = make_plan(
        PlanItem("delete_from_current", "old.tif", 10, "old", leaving_size=10),
        transfer_bytes=60,
    )

    with pytest.raises(SafetyError) as captured:
        assess_plan_safety(
            config,
            plan,
            disk_usage=lambda path: Usage(100, 50, 50),
        )

    message = str(captured.value)
    assert "max_deletes_per_run" in message
    assert "max_delete_size_gb" in message
    assert "insufficient archive space" in message


def test_projected_minimum_free_space_is_enforced(tmp_path: Path) -> None:
    config = make_config(tmp_path, SafetyConfig(1, 1, 20, 0, 0, 0.8))

    with pytest.raises(SafetyError, match="projected archive free space"):
        assess_plan_safety(
            config,
            make_plan(transfer_bytes=15),
            disk_usage=lambda path: Usage(100, 70, 30),
        )


@pytest.mark.parametrize(
    ("current_files", "current_bytes", "expected_gate"),
    [(700, 1000 * GIB, "catalogue_file_count_shrink"),
     (1000, 700 * GIB, "catalogue_bytes_shrink")],
)
def test_source_shrink_is_compared_with_latest_known_good(
    tmp_path: Path, current_files: int, current_bytes: int, expected_gate: str
) -> None:
    safety = replace(
        SafetyConfig(10, 10, 0, 0, 0, 0.8),
        max_source_file_count_drop=100,
        max_source_file_count_drop_percent=20,
        max_source_bytes_drop_gb=100,
        max_source_bytes_drop_percent=20,
    )
    plan = replace(make_plan(), catalogue_file_count=current_files,
                   catalogue_total_bytes=current_bytes)
    with pytest.raises(SafetyError) as captured:
        assess(make_config(tmp_path, safety), plan,
               SafetyReference("known_good", "backup-good", 1000, 1000 * GIB))
    assert expected_gate in {item.gate for item in captured.value.assessment.failures}
    assert captured.value.assessment.reference_run_id == "backup-good"


def test_baseline_is_supported_as_first_run_fallback(tmp_path: Path) -> None:
    safety = replace(SafetyConfig(10, 10, 0, 0, 0, 0.8),
                     max_source_file_count_drop_percent=10)
    plan = replace(make_plan(), catalogue_file_count=50, catalogue_total_bytes=100)
    with pytest.raises(SafetyError) as captured:
        assess(make_config(tmp_path, safety), plan,
               SafetyReference("baseline", None, 100, 100))
    assert captured.value.assessment.reference_kind == "baseline"


def test_high_changed_file_count_and_ratio_are_blocked(tmp_path: Path) -> None:
    safety = replace(SafetyConfig(10, 10, 0, 0, 0, 0.8),
                     max_changed_files_per_run=2,
                     max_changed_catalogue_percent=20)
    items = tuple(PlanItem("changed_file", f"{n}.tif", 1, "changed") for n in range(3))
    plan = replace(make_plan(*items, transfer_bytes=3),
                   catalogue_file_count=10, catalogue_total_bytes=10)
    with pytest.raises(SafetyError) as captured:
        assess(make_config(tmp_path, safety), plan,
               SafetyReference("known_good", "good", 10, 10))
    gates = {item.gate for item in captured.value.assessment.failures}
    assert {"high_change_count", "high_change_ratio"} <= gates


def test_high_changed_bytes_are_blocked(tmp_path: Path) -> None:
    safety = replace(SafetyConfig(10, 10, 0, 0, 0, 0.8), max_changed_bytes_gb=1)
    item = PlanItem("changed_file", "rewrite.tif", 2 * GIB, "changed")
    plan = replace(make_plan(item, transfer_bytes=2 * GIB),
                   catalogue_file_count=100, catalogue_total_bytes=2 * GIB)
    with pytest.raises(SafetyError) as captured:
        assess(make_config(tmp_path, safety), plan)
    assert "high_change_bytes" in {item.gate for item in captured.value.assessment.failures}


def test_small_change_and_delete_below_threshold_are_allowed(tmp_path: Path) -> None:
    safety = replace(SafetyConfig(2, 1, 0, 0, 0, 0.8),
                     max_changed_files_per_run=5, max_changed_bytes_gb=1,
                     max_changed_catalogue_percent=20)
    plan = replace(make_plan(
        PlanItem("changed_file", "edit.tif", 10, "changed", leaving_size=9),
        PlanItem("delete_from_current", "old.tif", 0, "old", leaving_size=8),
        transfer_bytes=10,
    ), catalogue_file_count=100, catalogue_total_bytes=1000)
    result = assess(make_config(tmp_path, safety), plan,
                    SafetyReference("known_good", "good", 100, 1000))
    assert result.failures == ()
    assert result.delete_count == 1
