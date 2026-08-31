"""Shared data contracts for game inspection and extraction workflows."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ArchiveInspection:
    """Read-only facts collected from one candidate game archive."""

    relative_path: str
    status: str
    reason: str
    size_bytes: int | None = None
    sha256: str | None = None
    signature: str | None = None
    signature_hex: str | None = None
    format_version: str | None = None
    entry_count: int | None = None
    toc_offset: int | None = None
    toc_size_bytes: int | None = None
    reserved: int | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class GameInspectionReport:
    """Stable, serializable result for a directory-level engine inspection."""

    schema_version: int
    engine: str
    root: str
    status: str
    archives: tuple[ArchiveInspection, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        status_counts = {}
        for archive in self.archives:
            status_counts[archive.status] = status_counts.get(archive.status, 0) + 1
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "root": self.root,
            "status": self.status,
            "summary": {
                "archive_count": len(self.archives),
                "status_counts": dict(sorted(status_counts.items())),
            },
            "archives": [archive.to_dict() for archive in self.archives],
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class ArchiveEntryInspection:
    """One decoded FilePack table-of-contents entry."""

    index: int
    internal_path: str
    filename_length_chars: int
    offset: int
    unknown1: int
    stored_size: int
    original_size: int
    compression_flag: int
    obfuscation_flag: int
    entry_hash: int
    data_within_bounds: bool

    def to_dict(self):
        payload = asdict(self)
        payload["entry_hash_hex"] = f"{self.entry_hash:08x}"
        return payload


@dataclass(frozen=True)
class QlieTocInspectionReport:
    """Stable report for a read-only supported QLIE FilePack TOC parse."""

    schema_version: int
    engine: str
    source_path: str
    status: str
    reason: str
    archive: ArchiveInspection
    hash_version: str | None = None
    obfuscation_seed: int | None = None
    entries: tuple[ArchiveEntryInspection, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "source_path": self.source_path,
            "status": self.status,
            "reason": self.reason,
            "archive": self.archive.to_dict(),
            "hash_version": self.hash_version,
            "obfuscation_seed": self.obfuscation_seed,
            "summary": {
                "declared_entry_count": self.archive.entry_count,
                "parsed_entry_count": len(self.entries),
                "compressed_entry_count": sum(bool(entry.compression_flag) for entry in self.entries),
                "obfuscated_entry_count": sum(bool(entry.obfuscation_flag) for entry in self.entries),
                "out_of_bounds_entry_count": sum(not entry.data_within_bounds for entry in self.entries),
            },
            "entries": [entry.to_dict() for entry in self.entries],
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class QlieEntryProbeReport:
    """Serializable diagnostics for one in-memory FilePack entry read."""

    schema_version: int
    engine: str
    source_path: str
    status: str
    reason: str
    selector_kind: str
    selector_value: str
    decode_stage: str
    entry: ArchiveEntryInspection | None = None
    key_source: str | None = None
    stored_sha256: str | None = None
    decoded_sha256: str | None = None
    stored_size: int | None = None
    decoded_size: int | None = None
    decoded_prefix_hex: str | None = None
    compression_applied: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "source_path": self.source_path,
            "status": self.status,
            "reason": self.reason,
            "selector": {
                "kind": self.selector_kind,
                "value": self.selector_value,
            },
            "decode_stage": self.decode_stage,
            "entry": self.entry.to_dict() if self.entry else None,
            "key_source": self.key_source,
            "stored_sha256": self.stored_sha256,
            "decoded_sha256": self.decoded_sha256,
            "stored_size": self.stored_size,
            "decoded_size": self.decoded_size,
            "decoded_prefix_hex": self.decoded_prefix_hex,
            "compression_applied": self.compression_applied,
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class QlieScriptExportItem:
    """One allowlisted script considered by a QLIE export plan."""

    archive_path: str
    archive_name: str
    archive_rank: int
    entry_index: int
    internal_path: str
    output_path: str | None
    status: str
    reason: str
    stored_size: int
    original_size: int
    compression_flag: int
    obfuscation_flag: int
    entry_hash: int
    shadowed_by: str | None = None
    conflict_group: str | None = None
    stored_sha256: str | None = None
    decoded_sha256: str | None = None
    decoded_size: int | None = None

    def to_dict(self):
        payload = asdict(self)
        payload["entry_hash_hex"] = f"{self.entry_hash:08x}"
        return payload


@dataclass(frozen=True)
class QlieScriptExportPlan:
    """Stable dry-run plan for allowlisted QLIE script export."""

    schema_version: int
    engine: str
    game_dir: str
    output_dir: str
    status: str
    reason: str
    archive_order: tuple[str, ...]
    archive_order_explicit: bool
    conflict_policy: str
    extensions: tuple[str, ...]
    items: tuple[QlieScriptExportItem, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        status_counts = {}
        for item in self.items:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
        selected = [
            item for item in self.items if item.status in {"selected", "preserved"}
        ]
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "game_dir": self.game_dir,
            "output_dir": self.output_dir,
            "status": self.status,
            "reason": self.reason,
            "archive_order": list(self.archive_order),
            "archive_order_explicit": self.archive_order_explicit,
            "conflict_policy": self.conflict_policy,
            "extensions": list(self.extensions),
            "summary": {
                "item_count": len(self.items),
                "selected_count": len(selected),
                "resolved_count": sum(item.status == "selected" for item in self.items),
                "preserved_count": sum(item.status == "preserved" for item in self.items),
                "estimated_decoded_bytes": sum(item.original_size for item in selected),
                "status_counts": dict(sorted(status_counts.items())),
            },
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class QlieScriptExportResult:
    """Result of an explicit, transactional QLIE script export."""

    schema_version: int
    engine: str
    status: str
    reason: str
    output_dir: str
    manifest_path: str | None
    items: tuple[QlieScriptExportItem, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        exported = [item for item in self.items if item.status == "exported"]
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "status": self.status,
            "reason": self.reason,
            "output_dir": self.output_dir,
            "manifest_path": self.manifest_path,
            "summary": {
                "exported_count": len(exported),
                "decoded_bytes": sum(item.decoded_size or 0 for item in exported),
            },
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class QlieScriptSurveyFile:
    """Content-free structural facts collected from one exported script."""

    output_path: str
    archive_name: str
    internal_path: str
    conflict_group: str | None
    status: str
    reason: str
    size_bytes: int | None = None
    expected_size: int | None = None
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    encoding: str | None = None
    has_bom: bool = False
    newline_style: str | None = None
    line_count: int = 0
    blank_line_count: int = 0
    max_line_length: int = 0
    nul_codepoint_count: int = 0
    line_family_counts: tuple[tuple[str, int], ...] = ()

    def to_dict(self):
        payload = asdict(self)
        payload["line_family_counts"] = dict(self.line_family_counts)
        return payload


@dataclass(frozen=True)
class QlieScriptSyntaxShape:
    """A redacted line shape and its aggregate occurrence count."""

    shape: str
    count: int

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class QlieScriptSurveyReport:
    """Stable report for a read-only survey of a Phase 1 export workspace."""

    schema_version: int
    engine: str
    export_dir: str
    manifest_path: str
    status: str
    reason: str
    manifest_item_count: int = 0
    files: tuple[QlieScriptSurveyFile, ...] = ()
    syntax_shapes: tuple[QlieScriptSyntaxShape, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        status_counts = {}
        encoding_counts = {}
        newline_style_counts = {}
        line_family_counts = {}
        archive_counts = {}
        extension_counts = {}
        for item in self.files:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
            if item.encoding:
                encoding_counts[item.encoding] = encoding_counts.get(item.encoding, 0) + 1
            if item.newline_style:
                newline_style_counts[item.newline_style] = (
                    newline_style_counts.get(item.newline_style, 0) + 1
                )
            for family, count in item.line_family_counts:
                line_family_counts[family] = line_family_counts.get(family, 0) + count
            if item.status == "surveyed":
                archive_counts[item.archive_name] = archive_counts.get(item.archive_name, 0) + 1
                extension = "." + item.output_path.rsplit(".", 1)[-1].casefold()
                extension_counts[extension] = extension_counts.get(extension, 0) + 1
        surveyed = [item for item in self.files if item.status == "surveyed"]
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "export_dir": self.export_dir,
            "manifest_path": self.manifest_path,
            "status": self.status,
            "reason": self.reason,
            "summary": {
                "manifest_item_count": self.manifest_item_count,
                "file_count": len(self.files),
                "surveyed_count": len(surveyed),
                "total_bytes": sum(item.size_bytes or 0 for item in surveyed),
                "total_lines": sum(item.line_count for item in surveyed),
                "blank_lines": sum(item.blank_line_count for item in surveyed),
                "conflict_variant_count": sum(
                    item.conflict_group is not None for item in surveyed
                ),
                "status_counts": dict(sorted(status_counts.items())),
                "archive_counts": dict(sorted(archive_counts.items())),
                "extension_counts": dict(sorted(extension_counts.items())),
                "encoding_counts": dict(sorted(encoding_counts.items())),
                "newline_style_counts": dict(sorted(newline_style_counts.items())),
                "line_family_counts": dict(sorted(line_family_counts.items())),
            },
            "syntax_shapes": [shape.to_dict() for shape in self.syntax_shapes],
            "files": [item.to_dict() for item in self.files],
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
