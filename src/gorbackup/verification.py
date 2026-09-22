"""Content verification for transferred files and explicit manual scopes."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator, Optional, Sequence, Tuple


CHUNK_SIZE = 4 * 1024 * 1024


class VerificationError(RuntimeError):
    """Raised when a verification request is unsafe or unusable."""


@dataclass(frozen=True)
class ExpectedSource:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class VerificationItem:
    path: str
    method: str
    status: str
    bytes_verified: int
    source_checksum: Optional[str]
    destination_checksum: Optional[str]
    detail: str


@dataclass(frozen=True)
class VerificationResult:
    method: str
    items: Tuple[VerificationItem, ...]

    @property
    def verified_files(self) -> int:
        return sum(item.status == "verified" for item in self.items)

    @property
    def verified_bytes(self) -> int:
        return sum(item.bytes_verified for item in self.items if item.status == "verified")

    @property
    def failures(self) -> int:
        return sum(item.status != "verified" for item in self.items)


def validate_relative_path(value: str) -> str:
    """Return a normalized POSIX relative path, rejecting ambiguous scopes."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise VerificationError(f"unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VerificationError(f"unsafe relative path: {value!r}")
    return path.as_posix()


def _safe_path(root: Path, relative_path: str, *, must_exist: bool = True) -> Path:
    relative_path = validate_relative_path(relative_path)
    resolved_root = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise VerificationError(f"cannot resolve {relative_path}: {exc}") from exc
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise VerificationError(f"path escapes configured root: {relative_path}")
    return resolved


def hash_file(path: Path, *, chunk_size: int = CHUNK_SIZE,
              opener: Callable[..., object] = open) -> Tuple[str, int]:
    """Stream *path* into SHA-256 without loading the file into memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    total = 0
    with opener(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _fingerprint(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
    return (stat_result.st_size, stat_result.st_mtime_ns,
            stat_result.st_dev, stat_result.st_ino)


def verify_file(source_root: Path, destination_root: Path, relative_path: str, *,
                expected: Optional[ExpectedSource] = None,
                hasher: Callable[[Path], Tuple[str, int]] = hash_file) -> VerificationItem:
    """Verify one path, distinguishing source races from destination corruption."""
    method = "sha256"
    try:
        source = _safe_path(source_root, relative_path)
        destination = _safe_path(destination_root, relative_path)
    except VerificationError as exc:
        return VerificationItem(relative_path, method, "error", 0, None, None, str(exc))
    try:
        source_before = source.stat()
        if not source.is_file():
            raise OSError("source is not a regular file")
        if expected is not None and (
            source_before.st_size != expected.size
            or source_before.st_mtime_ns != expected.mtime_ns
        ):
            return VerificationItem(
                relative_path, method, "source_changed", 0, None, None,
                "source metadata no longer matches the immutable plan",
            )
        destination_before = destination.stat()
        if not destination.is_file():
            raise OSError("destination is not a regular file")
        if source_before.st_size != destination_before.st_size:
            return VerificationItem(
                relative_path, method, "mismatch", 0, None, None,
                f"size mismatch: source={source_before.st_size} destination={destination_before.st_size}",
            )
        source_digest, source_bytes = hasher(source)
        source_after = source.stat()
        if _fingerprint(source_before) != _fingerprint(source_after):
            return VerificationItem(
                relative_path, method, "source_changed", 0, source_digest, None,
                "source changed while it was being hashed",
            )
        destination_digest, destination_bytes = hasher(destination)
        destination_after = destination.stat()
        if _fingerprint(destination_before) != _fingerprint(destination_after):
            return VerificationItem(
                relative_path, method, "error", 0, source_digest,
                destination_digest, "destination changed while it was being hashed",
            )
        if source_bytes != destination_bytes or source_digest != destination_digest:
            return VerificationItem(
                relative_path, method, "mismatch", 0, source_digest,
                destination_digest, "checksum mismatch",
            )
        return VerificationItem(
            relative_path, method, "verified", source_bytes, source_digest,
            destination_digest, "source and destination SHA-256 checksums match",
        )
    except (OSError, ValueError) as exc:
        return VerificationItem(relative_path, method, "error", 0, None, None, str(exc))


def verify_transfers(source_root: Path, destination_root: Path,
                     transfers: Iterable[object], *,
                     expected_sources: Optional[dict] = None,
                     hasher: Callable[[Path], Tuple[str, int]] = hash_file) -> VerificationResult:
    """Verify only path-level execution records whose operation is ``transfer``."""
    items = []
    for transfer in transfers:
        operation = transfer["operation"] if isinstance(transfer, dict) else transfer.operation
        if operation != "transfer":
            continue
        path = transfer["path"] if isinstance(transfer, dict) else transfer.path
        expected = expected_sources.get(path) if expected_sources else None
        items.append(verify_file(
            source_root, destination_root, path, expected=expected, hasher=hasher
        ))
    return VerificationResult("sha256", tuple(items))


def expand_manual_paths(source_root: Path, requested: Sequence[str]) -> Iterator[str]:
    """Expand selected files/directories recursively; an empty scope is forbidden."""
    if not requested:
        raise VerificationError("verify requires at least one relative file or directory path")
    seen = set()
    for value in requested:
        normalized = validate_relative_path(value)
        target = _safe_path(source_root, normalized)
        if target.is_file():
            candidates = (target,)
        elif target.is_dir():
            candidates = (item for item in target.rglob("*") if item.is_file())
        else:
            raise VerificationError(f"source path is not a file or directory: {normalized}")
        for item in candidates:
            relative = item.relative_to(source_root.resolve()).as_posix()
            _safe_path(source_root, relative)
            if relative not in seen:
                seen.add(relative)
                yield relative


def verify_selected(source_root: Path, destination_root: Path,
                    requested: Sequence[str]) -> VerificationResult:
    paths = tuple(expand_manual_paths(source_root, requested))
    transfers = ({"operation": "transfer", "path": path} for path in paths)
    return verify_transfers(source_root, destination_root, transfers)
