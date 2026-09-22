"""Versioned backup execution backed by independent rclone evidence."""

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from gorbackup.config import AppConfig
from gorbackup.dependencies import RcloneInfo
from gorbackup.ledger import Ledger, validate_state_location
from gorbackup.locking import BackupLock
from gorbackup.planner import PlanResult, _atomic_json, create_plan
from gorbackup.baseline import load_baseline_summary
from gorbackup.safety import (
    OVERRIDABLE_GATES,
    SafetyAssessment,
    SafetyError,
    SafetyReference,
    assess_plan_safety,
)
from gorbackup.verification import ExpectedSource, VerificationResult, verify_transfers


class BackupError(RuntimeError):
    """Raised when a backup cannot be completed or reconciled safely."""


@dataclass(frozen=True)
class ExecutionItem:
    operation: str
    classification: str
    path: str
    size: int
    message: str


@dataclass(frozen=True)
class Divergence:
    severity: str
    divergence_type: str
    operation: str
    path: str
    planned_bytes: Optional[int]
    executed_bytes: Optional[int]
    detail: str


@dataclass(frozen=True)
class BackupResult:
    run_id: str
    plan_run_id: str
    status: str
    executed_transfer_files: int
    executed_transfer_bytes: int
    executed_archive_files: int
    executed_archive_bytes: int
    safety: SafetyAssessment
    history_path: Path
    report_path: Path
    manifest_path: Path
    divergences: Tuple[Divergence, ...]
    verification: VerificationResult = field(
        default_factory=lambda: VerificationResult("sha256", ())
    )
    verification_report_path: Path = Path("")

    # Transitional API aliases; their meaning is always executed, never planned.
    @property
    def transferred_files(self) -> int:
        return self.executed_transfer_files

    @property
    def transferred_bytes(self) -> int:
        return self.executed_transfer_bytes

    @property
    def archived_files(self) -> int:
        return self.executed_archive_files

    @property
    def archived_bytes(self) -> int:
        return self.executed_archive_bytes


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rclone_literal_path(path: str) -> str:
    """Return an anchored rclone filter pattern matching one literal path."""
    escaped = path.replace("\\", "\\\\")
    for character in "*?[]{}":
        escaped = escaped.replace(character, "\\" + character)
    return "/" + escaped


def parse_execution_log(lines: Iterable[str]) -> Tuple[List[ExecutionItem], List[str], List[str]]:
    """Parse successful path operations from rclone's JSON log.

    ``gorbackup_operation`` and ``gorbackup_classification`` are accepted by
    synthetic fixtures. Production rclone entries are classified from its
    stable operation messages while retaining the original message verbatim.
    """
    items: List[ExecutionItem] = []
    warnings: List[str] = []
    errors: List[str] = []
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"invalid JSON execution log line {number}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"invalid execution log entry {number}")
            continue
        level = str(entry.get("level", "")).lower()
        message = str(entry.get("msg", ""))
        path = entry.get("object")
        size = entry.get("size", 0)
        if level in {"error", "fatal"}:
            errors.append(message or f"rclone error at line {number}")
            continue
        if level in {"warning", "warn"}:
            warnings.append(message or f"rclone warning at line {number}")
        operation = entry.get("gorbackup_operation")
        classification = entry.get("gorbackup_classification")
        if operation is None:
            normalized = message.lower()
            if normalized.startswith("copied"):
                operation = "transfer"
                classification = "replaced" if "replaced" in normalized else "new"
            elif normalized.startswith("moved"):
                operation = "archive"
                classification = "versioned"
            elif normalized.startswith("deleted"):
                # With --backup-dir a direct deletion violates the preservation contract.
                operation = "archive"
                classification = "deleted"
        if operation is None:
            continue
        if operation not in {"transfer", "archive"}:
            errors.append(f"unknown execution operation at line {number}: {operation!r}")
            continue
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts:
            errors.append(f"unsafe or missing execution path at line {number}")
            continue
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append(f"invalid execution size at line {number}")
            continue
        items.append(ExecutionItem(operation, str(classification or "unknown"), path, size, message))
    return items, warnings, errors


def _planned_operations(plan: PlanResult) -> Dict[Tuple[str, str], Tuple[int, str]]:
    operations: Dict[Tuple[str, str], Tuple[int, str]] = {}
    for item in plan.items:
        if item.category in {"new_file", "changed_file", "rename_move_candidate"}:
            expected = "new" if item.category == "new_file" else "replaced"
            operations[("transfer", item.path)] = (item.size, expected)
        if item.category in {"delete_from_current", "changed_file", "rename_move_candidate"}:
            archive_path = item.related_path if item.category == "rename_move_candidate" else item.path
            if archive_path:
                operations[("archive", archive_path)] = (item.leaving_size, "versioned")
    return operations


def reconcile_execution(plan: PlanResult, executed: Sequence[ExecutionItem]) -> Tuple[Divergence, ...]:
    """Compare immutable planned operations with independently logged execution."""
    planned = _planned_operations(plan)
    actual: Dict[Tuple[str, str], ExecutionItem] = {}
    divergences: List[Divergence] = []
    for item in executed:
        key = (item.operation, item.path)
        if key in actual:
            divergences.append(Divergence(
                "failure", "duplicate_execution", item.operation, item.path,
                planned.get(key, (None, ""))[0], item.size,
                "multiple successful execution events make totals ambiguous",
            ))
        actual[key] = item
    for key, (planned_bytes, planned_classification) in planned.items():
        item = actual.get(key)
        operation, path = key
        if item is None:
            divergences.append(Divergence(
                "warning", "planned_not_executed", operation, path, planned_bytes, None,
                "planned operation was absent from the successful execution log",
            ))
        elif item.size != planned_bytes:
            divergences.append(Divergence(
                "failure", "byte_mismatch", operation, path, planned_bytes, item.size,
                "executed byte count differs from the immutable plan",
            ))
        elif operation == "transfer" and item.classification not in {planned_classification, "unknown"}:
            divergences.append(Divergence(
                "warning", "classification_changed", operation, path, planned_bytes, item.size,
                f"planned {planned_classification} but rclone classified {item.classification}",
            ))
        elif operation == "archive" and item.classification == "deleted":
            divergences.append(Divergence(
                "failure", "not_versioned", operation, path, planned_bytes, item.size,
                "rclone deleted a path instead of preserving it in backup-dir",
            ))
    for key, item in actual.items():
        if key in planned:
            continue
        severity = "failure" if item.operation == "archive" else "warning"
        divergences.append(Divergence(
            severity, "unplanned_execution", item.operation, item.path, None, item.size,
            "execution performed an operation absent from the immutable plan",
        ))
    return tuple(divergences)


def audit_history(history_path: Path, executed: Sequence[ExecutionItem]) -> Tuple[Divergence, ...]:
    """Reconcile recorded archive events with files actually preserved in history."""
    recorded = {
        item.path: item.size
        for item in executed
        if item.operation == "archive" and item.classification != "deleted"
    }
    observed: Dict[str, int] = {}
    for path in history_path.rglob("*"):
        if path.is_file():
            observed[path.relative_to(history_path).as_posix()] = path.stat().st_size
    divergences: List[Divergence] = []
    for path, size in recorded.items():
        actual_size = observed.get(path)
        if actual_size is None:
            divergences.append(Divergence(
                "failure", "archive_missing_from_history", "archive", path,
                size, None, "rclone reported an archive event but history has no file",
            ))
        elif actual_size != size:
            divergences.append(Divergence(
                "failure", "archive_history_byte_mismatch", "archive", path,
                size, actual_size, "history file size differs from rclone execution evidence",
            ))
    for path, size in observed.items():
        if path not in recorded:
            divergences.append(Divergence(
                "failure", "unrecorded_history_file", "archive", path,
                None, size, "history contains a file absent from execution evidence",
            ))
    return tuple(divergences)


def run_backup(config: AppConfig, rclone: RcloneInfo, *,
               runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
               now: Callable[[], datetime] = _utc_now,
               run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
               planner: Callable[..., PlanResult] = create_plan,
               safety_checker: Callable[..., SafetyAssessment] = assess_plan_safety,
               override_safety: bool = False,
               manual_context: bool = False,
               lock_factory: Callable[..., BackupLock] = BackupLock,
               verifier: Callable[..., VerificationResult] = verify_transfers) -> BackupResult:
    """Run planning through persistence while holding exclusive backup ownership."""
    validate_state_location(config)
    lock = lock_factory(
        config.state_root / "backup.lock",
        run_id=f"backup-attempt-{uuid.uuid4().hex}",
        now=now,
    )
    with lock:
        return _run_backup_locked(
            config, rclone, runner=runner, now=now,
            run_id_factory=run_id_factory, planner=planner,
            safety_checker=safety_checker, override_safety=override_safety,
            manual_context=manual_context, verifier=verifier,
        )


def _run_backup_locked(config: AppConfig, rclone: RcloneInfo, *,
                       runner: Callable[..., subprocess.CompletedProcess],
                       now: Callable[[], datetime],
                       run_id_factory: Callable[[], str],
                       planner: Callable[..., PlanResult],
                       safety_checker: Callable[..., SafetyAssessment],
                       override_safety: bool,
                       manual_context: bool,
                       verifier: Callable[..., VerificationResult]) -> BackupResult:
    if override_safety and not manual_context:
        raise BackupError("--override-safety requires an interactive manual session")
    plan = planner(config, rclone, runner=runner, now=now)
    if plan.status != "success":
        raise BackupError(f"refusing to execute failed plan: {plan.run_id}")
    run_id, started_at = run_id_factory(), now()
    with Ledger(config.ledger_path) as ledger:
        known_good = ledger.known_good_state()
        if known_good is not None:
            reference = SafetyReference(
                "known_good", str(known_good["run_id"]),
                int(known_good["catalogue_file_count"]),
                int(known_good["catalogue_total_bytes"]),
            )
        else:
            baseline = load_baseline_summary(config)
            reference = (
                SafetyReference("baseline", None, baseline.file_count, baseline.total_size_bytes)
                if baseline is not None else None
            )
        ledger.start_run(run_id, started_at.isoformat(), config.source.marker_id,
                         config.archive.marker_id, operation="backup")
        try:
            safety = safety_checker(config, plan, reference=reference)
        except SafetyError as exc:
            assessment = exc.assessment
            failures = assessment.failures if assessment is not None else ()
            failed_gates = [item.gate for item in failures]
            non_overridable = [item for item in failures if not item.overridable]
            can_override = bool(failures) and not non_overridable and set(failed_gates) <= OVERRIDABLE_GATES
            override_used = override_safety and manual_context and can_override
            payload = asdict(assessment) if assessment is not None else {"reasons": list(exc.reasons)}
            message = (
                "manual safety override accepted: " + ", ".join(failed_gates)
                if override_used else str(exc)
            )
            ledger.record_safety_assessment(
                run_id, plan.run_id, payload, failed_gates,
                override_requested=override_safety, override_used=override_used,
                overridden_gates=failed_gates if override_used else (), message=message,
            )
            if not override_used:
                ledger.block_run(run_id, now().isoformat(), plan.run_id, message)
                raise
            safety = assessment
        else:
            ledger.record_safety_assessment(
                run_id, plan.run_id, asdict(safety), (),
                override_requested=override_safety, override_used=False,
                overridden_gates=(), message="all safety gates passed",
            )

    current = config.archive.root / config.archive.current_dir
    history_path = config.archive.root / config.archive.history_dir / run_id
    try:
        history_path.mkdir()
    except FileExistsError as exc:
        raise BackupError(f"history target already exists: {history_path}") from exc
    except OSError as exc:
        raise BackupError(f"could not reserve history target {history_path}: {exc}") from exc

    config.state_root.mkdir(parents=True, exist_ok=True)
    descriptor, log_name = tempfile.mkstemp(prefix=".backup-log-", suffix=".jsonl", dir=str(config.state_root))
    os.close(descriptor)
    log_path = Path(log_name)
    report_path = config.manifests_root / f"execution-{run_id}.json"
    verification_report_path = config.manifests_root / f"verification-{run_id}.json"
    command = [
        rclone.executable, "sync", str(config.source.path), str(current),
        "--use-json-log", "--log-level", "INFO", "--log-file", str(log_path),
        "--backup-dir", str(history_path), "--max-delete", str(config.safety.max_deletes_per_run),
        "--max-delete-size", f"{int(config.safety.max_delete_size_gb * 1024 ** 3)}B",
        "--exclude", f"/{config.source.marker_file}", "--retries", "1",
    ]
    if config.safety.ignore_recent_minutes:
        command.extend(["--min-age", f"{config.safety.ignore_recent_minutes}m"])
    for item in plan.items:
        if item.category == "skipped_recent":
            # Pin the immutable plan's grace decision. A file close to the age
            # boundary must not become transferable during this same run.
            command.extend(["--exclude", _rclone_literal_path(item.path)])

    with Ledger(config.ledger_path) as ledger:
        try:
            completed = runner(command, check=False, capture_output=True, text=True)
            executed, log_warnings, log_errors = parse_execution_log(log_path.read_text(encoding="utf-8").splitlines())
            if completed.returncode != 0 and not log_errors:
                log_errors.append((completed.stderr or completed.stdout).strip() or f"rclone exited with {completed.returncode}")
            divergences = reconcile_execution(plan, executed) + audit_history(history_path, executed)
            errors = list(log_errors) + [item.detail for item in divergences if item.severity == "failure"]
            warnings = list(log_warnings) + [item.detail for item in divergences if item.severity == "warning"]
            ledger.record_execution_evidence(
                run_id, plan.run_id, str(history_path), str(report_path),
                [asdict(item) for item in executed], [asdict(item) for item in divergences],
            )
            # Read back the durable execution evidence: planned-only paths are
            # deliberately incapable of entering the verification scope.
            actual_items = ledger.execution_items(run_id)
            expected_sources = {
                row["relative_path"]: ExpectedSource(row["size"], row["mtime_ns"])
                for row in ledger.connection.execute(
                    "SELECT relative_path, size, mtime_ns FROM plan_catalogue_files WHERE run_id=?",
                    (plan.run_id,),
                )
            }
            verification = verifier(
                config.source.path, current, actual_items,
                expected_sources=expected_sources,
            )
            verification_errors = [
                f"verification_{item.status}: {item.path}: {item.detail}"
                for item in verification.items if item.status != "verified"
            ]
            errors.extend(verification_errors)
            status = "failed" if completed.returncode != 0 or errors else "warning" if warnings else "success"
            completed_at = now()
            verification_payload = {
                "schema_version": 1, "run_id": run_id,
                "method": verification.method,
                "verified_files": verification.verified_files,
                "verified_bytes": verification.verified_bytes,
                "verification_failures": verification.failures,
                "items": [asdict(item) for item in verification.items],
            }
            _atomic_json(verification_report_path, verification_payload)
            report = {
                "schema_version": 3, "run_id": run_id, "plan_run_id": plan.run_id,
                "status": status, "started_at": started_at.isoformat(), "completed_at": completed_at.isoformat(),
                "items": [asdict(item) for item in executed],
                "divergences": [asdict(item) for item in divergences],
                "verification_report": str(verification_report_path),
                "warnings": warnings, "errors": errors,
            }
            _atomic_json(report_path, report)
            ledger.finalize_execution(
                run_id, completed_at.isoformat(), status,
                [asdict(item) for item in verification.items], warnings, errors,
            )
        except Exception as exc:
            ledger.fail_run(run_id, now().isoformat(), str(exc))
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"backup execution failed: {exc}") from exc
        finally:
            if log_path.exists():
                log_path.unlink()

    transfers = [item for item in executed if item.operation == "transfer"]
    archives = [item for item in executed if item.operation == "archive"]
    manifest_path = config.manifests_root / f"backup-{run_id}.json"
    payload = {
        "schema_version": 3, "run_id": run_id, "plan_run_id": plan.run_id, "status": status,
        "started_at": started_at.isoformat(), "completed_at": completed_at.isoformat(),
        "history_path": str(history_path), "execution_report": str(report_path),
        "catalogue_file_count": plan.catalogue_file_count, "catalogue_total_bytes": plan.catalogue_total_bytes,
        "planned_transfer_files": plan.planned_transfer_files, "planned_transfer_bytes": plan.planned_transfer_bytes,
        "planned_archive_files": plan.planned_archive_files, "planned_archive_bytes": plan.planned_archive_bytes,
        "executed_transfer_files": len(transfers), "executed_transfer_bytes": sum(i.size for i in transfers),
        "executed_archive_files": len(archives), "executed_archive_bytes": sum(i.size for i in archives),
        "verification_method": verification.method,
        "verified_files": verification.verified_files,
        "verified_bytes": verification.verified_bytes,
        "verification_failures": verification.failures,
        "verification_report": str(verification_report_path),
        "warnings": warnings, "divergences": [asdict(item) for item in divergences], "safety": asdict(safety),
    }
    try:
        _atomic_json(manifest_path, payload)
        _atomic_json(config.manifests_root / "latest-backup.json", payload)
    except OSError as exc:
        with Ledger(config.ledger_path) as ledger:
            ledger.invalidate_completed_run(run_id, str(exc))
        raise BackupError(f"backup completed but manifest could not be written: {exc}") from exc
    if status == "success":
        with Ledger(config.ledger_path) as ledger:
            ledger.promote_known_good(run_id)
    if status == "failed":
        raise BackupError(
            "backup execution or verification failed; see " + str(report_path)
        )
    return BackupResult(run_id, plan.run_id, status, len(transfers), sum(i.size for i in transfers),
                        len(archives), sum(i.size for i in archives), safety, history_path,
                        report_path, manifest_path, divergences, verification,
                        verification_report_path)
