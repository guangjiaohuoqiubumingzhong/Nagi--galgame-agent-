"""Read-only inspection for QLIE FilePack archives.

QLIE stores its FilePack trailer at the end of each ``.pack`` file.  Phase 0
only reads that fixed-size trailer and optionally streams the file through
SHA-256.  It does not parse the table of contents, extract entries, or write to
the inspected directory.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

from ..models import ArchiveInspection, GameInspectionReport


FILEPACK_TRAILER = struct.Struct("<16sIII")
FILEPACK_SIGNATURES = {
    b"FilePackVer1.0".ljust(16, b"\x00"): "1.0",
    b"FilePackVer3.0".ljust(16, b"\x00"): "3.0",
    b"FilePackVer3.1".ljust(16, b"\x00"): "3.1",
}
HASH_CHUNK_SIZE = 1024 * 1024
REPORT_SCHEMA_VERSION = 1


def _relative_display_path(path, root=None):
    path = Path(path)
    if root is not None:
        try:
            return path.relative_to(Path(root)).as_posix()
        except ValueError:
            pass
    return path.name


def _sha256_stream(stream):
    digest = hashlib.sha256()
    stream.seek(0)
    while True:
        chunk = stream.read(HASH_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _unreadable_result(path, root, reason):
    return ArchiveInspection(
        relative_path=_relative_display_path(path, root),
        status="unreadable",
        reason=reason,
    )


def inspect_archive(path, root=None, hash_file=True):
    """Inspect one candidate archive without changing it.

    Unknown, short, missing, and unreadable files are represented as structured
    results so callers can report a complete directory scan.
    """

    path = Path(path)
    display_path = _relative_display_path(path, root)
    try:
        if path.is_symlink():
            return ArchiveInspection(
                relative_path=display_path,
                status="unsupported",
                reason="symbolic links are not inspected",
            )
        stat_before = path.stat()
        if not path.is_file():
            return _unreadable_result(path, root, "path is not a regular file")
        with path.open("rb") as stream:
            sha256 = _sha256_stream(stream) if hash_file else None
            if stat_before.st_size < FILEPACK_TRAILER.size:
                return ArchiveInspection(
                    relative_path=display_path,
                    status="invalid",
                    reason=f"file is shorter than the {FILEPACK_TRAILER.size}-byte FilePack trailer",
                    size_bytes=stat_before.st_size,
                    sha256=sha256,
                )
            stream.seek(-FILEPACK_TRAILER.size, os.SEEK_END)
            trailer = stream.read(FILEPACK_TRAILER.size)
    except (OSError, PermissionError) as exc:
        return _unreadable_result(path, root, f"unable to read archive: {exc}")

    try:
        stat_after = path.stat()
    except OSError as exc:
        return _unreadable_result(path, root, f"unable to restat archive: {exc}")
    if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns):
        return ArchiveInspection(
            relative_path=display_path,
            status="changed",
            reason="archive changed while it was being inspected",
            size_bytes=stat_after.st_size,
            sha256=sha256,
        )

    signature_bytes, entry_count, toc_offset, reserved = FILEPACK_TRAILER.unpack(trailer)
    signature = signature_bytes.rstrip(b"\x00").decode("ascii", errors="replace")
    version = FILEPACK_SIGNATURES.get(signature_bytes)
    common = {
        "relative_path": display_path,
        "size_bytes": stat_before.st_size,
        "sha256": sha256,
        "signature": signature,
        "signature_hex": signature_bytes.hex(),
        "format_version": version,
        "entry_count": entry_count,
        "toc_offset": toc_offset,
        "reserved": reserved,
    }
    if version is None:
        return ArchiveInspection(
            **common,
            status="unsupported",
            reason="trailer does not contain a supported QLIE FilePack signature",
        )

    trailer_offset = stat_before.st_size - FILEPACK_TRAILER.size
    if toc_offset > trailer_offset:
        return ArchiveInspection(
            **common,
            status="invalid",
            reason="table-of-contents offset points beyond the FilePack trailer",
        )

    return ArchiveInspection(
        **common,
        status="supported",
        reason=f"recognized QLIE FilePackVer{version} trailer",
        toc_size_bytes=trailer_offset - toc_offset,
    )


def _candidate_paths(root, warnings):
    candidates = []

    def record_walk_error(exc):
        warnings.append(f"unable to scan {exc.filename or root}: {exc}")

    walker = os.walk(
        root,
        topdown=True,
        onerror=record_walk_error,
        followlinks=False,
    )
    for current_root, directory_names, file_names in walker:
        directory_names[:] = sorted(directory_names, key=str.casefold)
        for file_name in file_names:
            if Path(file_name).suffix.lower() == ".pack":
                candidates.append(Path(current_root) / file_name)
    return sorted(candidates, key=lambda item: item.relative_to(root).as_posix().casefold())


def inspect_game_directory(root, hash_files=True):
    """Inspect every ``.pack`` candidate below a game directory."""

    root = Path(root).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"game directory does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"game path is not a directory: {root}")

    warnings = []
    archives = tuple(
        inspect_archive(path, root=root, hash_file=hash_files)
        for path in _candidate_paths(root, warnings)
    )
    supported_count = sum(archive.status == "supported" for archive in archives)
    if not archives:
        status = "no_candidates"
    elif supported_count == len(archives):
        status = "supported"
    elif supported_count:
        status = "partial"
    else:
        status = "unsupported"

    return GameInspectionReport(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        root=str(root),
        status=status,
        archives=archives,
        warnings=tuple(warnings),
    )


def render_report_text(report):
    """Render a compact human-readable summary of an inspection report."""

    counts = report.to_dict()["summary"]["status_counts"]
    count_text = ", ".join(f"{name}={count}" for name, count in counts.items()) or "none"
    lines = [
        f"QLIE inspection: {report.status}",
        f"root: {report.root}",
        f"archives: {len(report.archives)} ({count_text})",
    ]
    for archive in report.archives:
        version = f"FilePackVer{archive.format_version}" if archive.format_version else archive.signature or "unknown"
        hash_text = archive.sha256 or "skipped"
        details = [archive.status, version, f"size={archive.size_bytes}", f"sha256={hash_text}"]
        if archive.entry_count is not None:
            details.append(f"entries={archive.entry_count}")
        lines.append(f"- {archive.relative_path}: " + ", ".join(details))
        if archive.status != "supported":
            lines.append(f"  reason: {archive.reason}")
    lines.extend(f"warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)
