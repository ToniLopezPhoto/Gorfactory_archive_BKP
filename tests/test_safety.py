from collections import namedtuple
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
from gorbackup.safety import GIB, SafetyError, assess_plan_safety

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
