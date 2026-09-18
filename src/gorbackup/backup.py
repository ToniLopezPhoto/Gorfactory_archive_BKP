"""Versioned, non-destructive incremental backup execution."""

import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from gorbackup.config import AppConfig
from gorbackup.dependencies import RcloneInfo
from gorbackup.ledger import Ledger, validate_state_location
from gorbackup.planner import PlanResult, _atomic_json, create_plan, parse_json_log
from gorbackup.safety import SafetyAssessment, assess_plan_safety


class BackupError(RuntimeError):
    """Raised when a backup cannot be completed safely."""


@dataclass(frozen=True)
class BackupResult:
    run_id: str
    plan_run_id: str
    status: str
    transferred_files: int
    transferred_bytes: int
    archived_files: int
    archived_bytes: int
    safety: SafetyAssessment
    history_path: Path
    manifest_path: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_backup(
    config: AppConfig,
    rclone: RcloneInfo,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    now: Callable[[], datetime] = _utc_now,
    run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    planner: Callable[..., PlanResult] = create_plan,
    safety_checker: Callable[..., SafetyAssessment] = assess_plan_safety,
) -> BackupResult:
    """Plan and execute one sync, preserving displaced files by run ID."""
    validate_state_location(config)
    plan = planner(config, rclone, runner=runner, now=now)
    if plan.status != "success":
        raise BackupError(f"refusing to execute failed plan: {plan.run_id}")
    safety = safety_checker(config, plan)

    run_id = run_id_factory()
    started_at = now()
    current = config.archive.root / config.archive.current_dir
    history_path = config.archive.root / config.archive.history_dir / run_id
    try:
        history_path.mkdir()
    except FileExistsError as exc:
        raise BackupError(f"history target already exists: {history_path}") from exc
    except OSError as exc:
        raise BackupError(f"could not reserve history target {history_path}: {exc}") from exc

    config.state_root.mkdir(parents=True, exist_ok=True)
    descriptor, log_name = tempfile.mkstemp(
        prefix=".backup-log-", suffix=".jsonl", dir=str(config.state_root)
    )
    os.close(descriptor)
    log_path = Path(log_name)
    command = [
        rclone.executable,
        "sync",
        str(config.source.path),
        str(current),
        "--use-json-log",
        "--log-level",
        config.logging.level,
        "--log-file",
        str(log_path),
        "--backup-dir",
        str(history_path),
        "--max-delete",
        str(config.safety.max_deletes_per_run),
        "--max-delete-size",
        f"{int(config.safety.max_delete_size_gb * 1024 ** 3)}B",
        "--exclude",
        f"/{config.source.marker_file}",
        "--retries",
        "1",
    ]
    if config.safety.ignore_recent_minutes:
        command.extend(["--min-age", f"{config.safety.ignore_recent_minutes}m"])

    transferred_files = sum(
        plan.counts[name]
        for name in ("new_file", "changed_file", "rename_move_candidate")
    )
    archived_files = sum(
        plan.counts[name]
        for name in ("delete_from_current", "changed_file", "rename_move_candidate")
    )
    warnings = []
    with Ledger(config.ledger_path) as ledger:
        ledger.start_run(
            run_id,
            started_at.isoformat(),
            config.source.marker_id,
            config.archive.marker_id,
            operation="backup",
        )
        try:
            completed = runner(command, check=False, capture_output=True, text=True)
            log_errors, warnings = parse_json_log(
                log_path.read_text(encoding="utf-8").splitlines()
            )
            if completed.returncode != 0 or log_errors:
                details = [item.reason for item in log_errors]
                if not details:
                    details.append(
                        (completed.stderr or completed.stdout).strip()
                        or f"rclone exited with {completed.returncode}"
                    )
                raise BackupError("; ".join(details))
            completed_at = now()
            ledger.complete_backup(
                run_id,
                plan.run_id,
                completed_at.isoformat(),
                str(history_path),
                transferred_files,
                plan.transfer_bytes,
                plan.leaving_current_bytes,
                warnings,
            )
        except Exception as exc:
            ledger.fail_run(run_id, now().isoformat(), str(exc))
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"backup execution failed: {exc}") from exc
        finally:
            if log_path.exists():
                log_path.unlink()

    manifest_path = config.manifests_root / f"backup-{run_id}.json"
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "plan_run_id": plan.run_id,
        "status": "success",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "history_path": str(history_path),
        "transferred_files": transferred_files,
        "transferred_bytes": plan.transfer_bytes,
        "archived_files": archived_files,
        "archived_bytes": plan.leaving_current_bytes,
        "warnings": warnings,
        "safety": asdict(safety),
        "plan": [asdict(item) for item in plan.items],
    }
    try:
        _atomic_json(manifest_path, payload)
        _atomic_json(config.manifests_root / "latest-backup.json", payload)
    except OSError as exc:
        raise BackupError(
            f"backup succeeded but manifest could not be written: {exc}"
        ) from exc
    return BackupResult(
        run_id,
        plan.run_id,
        "success",
        transferred_files,
        plan.transfer_bytes,
        archived_files,
        plan.leaving_current_bytes,
        safety,
        history_path,
        manifest_path,
    )
