import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gorbackup.config import (AppConfig, ArchiveConfig, LoggingConfig,
                              RetentionConfig, SafetyConfig, SourceConfig,
                              StateConfig)
from gorbackup.ledger import Ledger
from gorbackup.locking import BackupLock, HistoryLock, LockError
from gorbackup.pruning import PruneError, execute_prune, plan_prune
from gorbackup.recovery import RecoveryError, find_history, restore_version


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def config(tmp_path: Path, *, days: int = 365, runs: int = 0) -> AppConfig:
    source = tmp_path / "source"
    archive = tmp_path / "archive"
    source.mkdir()
    (archive / "current").mkdir(parents=True)
    (archive / "history").mkdir()
    (archive / ".archive").write_text("archive\n")
    return AppConfig(
        SourceConfig(source, source, ".source", "source"),
        ArchiveConfig(archive, "current", "history", ".archive", "archive"),
        SafetyConfig(10, 1, 0, 0, 0, .8), RetentionConfig(False, days, runs),
        LoggingConfig(), StateConfig(Path("state")),
    )


def seed(cfg: AppConfig, run_id: str, relative: str, data: bytes,
         when: str, *, physical: bool = True, status: str = "success") -> Path:
    path = cfg.archive.root / "history" / run_id / relative
    if physical:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    with Ledger(cfg.ledger_path) as ledger, ledger.connection:
        ledger.connection.execute(
            "INSERT INTO runs (run_id,operation,started_at,completed_at,status,source_identity,destination_identity) VALUES (?,?,?,?,?,'s','a')",
            ("plan-" + run_id, "plan", when, when, "success"))
        ledger.connection.execute("INSERT INTO plans VALUES (?,?, 'success','{}','{}')",
                                  ("plan-" + run_id, when))
        ledger.connection.execute(
            "INSERT INTO runs (run_id,operation,started_at,completed_at,status,source_identity,destination_identity) VALUES (?,'backup',?,?,?,'s','a')",
            (run_id, when, when, status))
        ledger.connection.execute("INSERT INTO executions VALUES (?,?,?,'report','exact')",
                                  (run_id, "plan-" + run_id,
                                   str(cfg.archive.root / "history" / run_id)))
        ledger.connection.execute(
            "INSERT INTO execution_items (run_id,operation,classification,path,size,message) VALUES (?,'archive','versioned',?,?,'archived')",
            (run_id, relative, len(data)))
    return path


def plan(cfg: AppConfig, prune_id: str = "prune-1"):
    return plan_prune(cfg, now=lambda: NOW, prune_id_factory=lambda: prune_id)


def test_dry_run_is_ordered_and_respects_age_minimum_and_known_good(tmp_path: Path) -> None:
    cfg = config(tmp_path, runs=1)
    seed(cfg, "old", "z.tif", b"old", "2020-01-01T00:00:00+00:00")
    seed(cfg, "middle", "a.tif", b"mid", "2021-01-01T00:00:00+00:00")
    seed(cfg, "recent", "r.tif", b"new", "2026-09-01T00:00:00+00:00")
    with Ledger(cfg.ledger_path) as ledger, ledger.connection:
        ledger.connection.execute(
            "INSERT INTO known_good_state VALUES (1,'middle',1,3,?)", (NOW.isoformat(),))
    result = plan(cfg)
    payload = json.loads(result.manifest_path.read_text())
    assert [(item["source_run_id"], item["relative_path"]) for item in payload["items"]] == [("old", "z.tif")]
    reasons = " ".join(item["reasons"] for item in payload["protected"])
    assert "latest_known_good_run" in reasons
    assert "keep_min_runs" in reasons
    assert "too_recent" in reasons


def test_missing_before_prune_is_not_candidate_or_pruned(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    seed(cfg, "old", "missing.tif", b"x", "2020-01-01T00:00:00+00:00", physical=False)
    result = plan(cfg)
    payload = json.loads(result.manifest_path.read_text())
    assert result.files == 0
    assert payload["missing_before_prune"][0]["reason"] == "missing_before_prune"


def test_successful_prune_retains_unknown_content_and_marks_history(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    historical = seed(cfg, "old", "Campaña Otoño/Selección José 001.tif", b"old",
                      "2020-01-01T00:00:00+00:00")
    unknown = historical.parents[1] / "unknown.txt"
    unknown.write_text("keep")
    plan(cfg)
    result = execute_prune(cfg, "prune-1", yes=True, now=lambda: NOW)
    assert result.status == "success" and not historical.exists()
    assert unknown.read_text() == "keep"
    execution = json.loads(result.manifest_path.read_text())
    assert "untracked history content prevents directory cleanup" in execution["cleanup_warnings"][0]
    history = find_history(cfg, "Campaña Otoño/Selección José 001.tif")
    assert history.versions[0].pruned_by == "prune-1"
    with pytest.raises(RecoveryError, match="deliberately pruned by prune-1"):
        restore_version(cfg, history.relative_path, "old")
    with Ledger(cfg.ledger_path) as ledger:
        assert ledger.execution_items("old")
        assert ledger.prune_items("prune-1")[0]["status"] == "deleted"


def test_stale_plan_aborts_before_any_delete(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    first = seed(cfg, "old1", "one.tif", b"one", "2020-01-01T00:00:00+00:00")
    second = seed(cfg, "old2", "two.tif", b"two", "2020-01-02T00:00:00+00:00")
    plan(cfg)
    second.write_bytes(b"changed")
    with pytest.raises(PruneError, match="stale prune plan"):
        execute_prune(cfg, "prune-1", yes=True)
    assert first.exists() and second.exists()


@pytest.mark.parametrize("bad", ["../../current/x", "/tmp/x"])
def test_manipulated_plan_path_is_rejected(tmp_path: Path, bad: str) -> None:
    cfg = config(tmp_path)
    original = seed(cfg, "old", "x.tif", b"x", "2020-01-01T00:00:00+00:00")
    result = plan(cfg)
    payload = json.loads(result.manifest_path.read_text())
    payload["items"][0]["historical_path"] = bad
    result.manifest_path.write_text(json.dumps(payload))
    with pytest.raises(PruneError, match="immutable ledger evidence"):
        execute_prune(cfg, "prune-1", yes=True)
    assert original.exists()


def test_symlink_escape_is_rejected_and_outside_is_intact(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    path = seed(cfg, "old", "x.tif", b"x", "2020-01-01T00:00:00+00:00")
    outside = tmp_path / "outside"
    outside.write_bytes(b"safe")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(PruneError, match="symlink"):
        plan(cfg)
    assert outside.read_bytes() == b"safe"


def test_new_failed_restore_protection_makes_plan_stale(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    path = seed(cfg, "old", "x.tif", b"x", "2020-01-01T00:00:00+00:00")
    plan(cfg)
    with Ledger(cfg.ledger_path) as ledger:
        ledger.start_restore("restore-1", NOW.isoformat(), "x.tif", "old", str(path), "dest")
        ledger.finish_restore("restore-1", NOW.isoformat(), status="failed", error="test")
    with pytest.raises(PruneError, match="became protected"):
        execute_prune(cfg, "prune-1", yes=True)
    assert path.exists()


def test_history_and_backup_locks_block_prune(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    seed(cfg, "old", "x.tif", b"x", "2020-01-01T00:00:00+00:00")
    plan(cfg)
    with HistoryLock(cfg.state_root / ".history-access.lock", exclusive=False):
        with pytest.raises(LockError):
            execute_prune(cfg, "prune-1", yes=True)
    with BackupLock(cfg.state_root / "backup.lock", run_id="backup"):
        with pytest.raises(LockError):
            execute_prune(cfg, "prune-1", yes=True)


def test_partial_failure_records_deleted_then_failed(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    first = seed(cfg, "old1", "one.tif", b"one", "2020-01-01T00:00:00+00:00")
    second = seed(cfg, "old2", "two.tif", b"two", "2020-01-02T00:00:00+00:00")
    plan(cfg)
    calls = []
    def unlink(path: Path) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError("injected failure")
        path.unlink()
    with pytest.raises(PruneError, match="injected failure"):
        execute_prune(cfg, "prune-1", yes=True, now=lambda: NOW, unlinker=unlink)
    assert not first.exists() and second.exists()
    with Ledger(cfg.ledger_path) as ledger:
        assert ledger.prune_run("prune-1")["status"] == "failed"
        assert [item["status"] for item in ledger.prune_items("prune-1")] == ["deleted", "failed"]


def test_completed_plan_cannot_be_executed_twice_and_yes_is_required(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    seed(cfg, "old", "x.tif", b"x", "2020-01-01T00:00:00+00:00")
    plan(cfg)
    with pytest.raises(PruneError, match="requires explicit --yes"):
        execute_prune(cfg, "prune-1", yes=False)
    execute_prune(cfg, "prune-1", yes=True)
    with pytest.raises(PruneError, match="not executable"):
        execute_prune(cfg, "prune-1", yes=True)
