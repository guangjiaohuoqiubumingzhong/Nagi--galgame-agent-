"""Read-only QLIE FilePack table-of-contents parsing.

This module decodes archive filenames and entry metadata only.  It never reads
entry payloads and never writes extracted files.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from ..models import ArchiveEntryInspection, QlieTocInspectionReport
from .detector import FILEPACK_TRAILER, REPORT_SCHEMA_VERSION, inspect_archive


PACK_ENTRY = struct.Struct("<IIIIIII")
PACK_ENTRY_WITHOUT_HASH = struct.Struct("<IIIIII")
HASH_HEADER = struct.Struct("<16sIIII")
MAX_TOC_SIZE = 64 * 1024 * 1024
MAX_ENTRY_COUNT = 1_000_000
HASH_SEED_BYTES = 256
WORD_ADD_CONSTANT = 0xA35793A7A35793A7
WORD_ADD_CONSTANT_V30 = 0x0307030703070307


class TocParseError(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _add_u16_lanes(left, right):
    result = 0
    for shift in (0, 16, 32, 48):
        word = ((left >> shift) & 0xFFFF) + ((right >> shift) & 0xFFFF)
        result |= (word & 0xFFFF) << shift
    return result


def _rotate_left_32_lanes(value, amount):
    amount &= 31
    low = value & 0xFFFFFFFF
    high = (value >> 32) & 0xFFFFFFFF
    low = ((low << amount) | (low >> (32 - amount))) & 0xFFFFFFFF
    high = ((high << amount) | (high >> (32 - amount))) & 0xFFFFFFFF
    return low | (high << 32)


def _signed_u16(value):
    return value - 0x10000 if value & 0x8000 else value


def _filename_seed31(hash_bytes):
    """Reproduce the FilePackVer3.1 MMX seed calculation."""

    if len(hash_bytes) < HASH_SEED_BYTES:
        raise TocParseError("invalid", "hash seed region is shorter than 256 bytes")
    key = 0
    result = 0
    for offset in range(0, HASH_SEED_BYTES, 8):
        key = _add_u16_lanes(key, WORD_ADD_CONSTANT)
        block = int.from_bytes(hash_bytes[offset : offset + 8], "little")
        result = _add_u16_lanes(result, block ^ key)
        result = _rotate_left_32_lanes(result, 3)

    words = [_signed_u16((result >> shift) & 0xFFFF) for shift in (0, 16, 32, 48)]
    checksum = (words[0] * words[2] + words[1] * words[3]) & 0xFFFFFFFF
    return checksum & 0x0FFFFFFF


def _filename_seed30(hash_bytes):
    """Reproduce the FilePackVer3.0 MMX seed calculation."""

    if len(hash_bytes) < HASH_SEED_BYTES:
        raise TocParseError("invalid", "hash seed region is shorter than 256 bytes")
    key = 0
    result = 0
    for offset in range(0, HASH_SEED_BYTES, 8):
        key = _add_u16_lanes(key, WORD_ADD_CONSTANT_V30)
        block = int.from_bytes(hash_bytes[offset : offset + 8], "little")
        result = _add_u16_lanes(result, block ^ key)
    return ((result & 0xFFFFFFFF) ^ (result >> 32)) & 0x0FFFFFFF


def _decode_filename31(encoded, seed):
    if len(encoded) % 2:
        raise TocParseError("invalid", "encoded UTF-16 filename has an odd byte length")
    char_count = len(encoded) // 2
    mutator = seed ^ ((seed >> 16) & 0xFFFF)
    key = ((char_count * char_count) ^ (char_count ^ 0x3E13) ^ mutator) & 0xFFFF
    current_key = key
    decoded = bytearray(len(encoded))
    for index in range(char_count):
        current_key = ((current_key << 3) + index + key) & 0xFFFF
        word = struct.unpack_from("<H", encoded, index * 2)[0] ^ current_key
        struct.pack_into("<H", decoded, index * 2, word)
    return bytes(decoded)


def _decode_filename30(encoded, seed):
    key = (len(encoded) + (seed ^ 0x3E)) & 0xFF
    decoded = bytearray(encoded)
    for index in range(1, len(decoded) + 1):
        decoded[index - 1] ^= (((index ^ key) & 0xFF) + index) & 0xFF
    return bytes(decoded)


def _decode_legacy_filename(encoded, *, include_length):
    key = 0xC4 ^ 0x3E
    if include_length:
        key += len(encoded)
    decoded = bytearray(encoded)
    for index in range(1, len(decoded) + 1):
        decoded[index - 1] ^= (((index ^ key) & 0xFF) + index) & 0xFF
    return bytes(decoded)


def _require_range(data, offset, length, label):
    if offset < 0 or length < 0 or offset + length > len(data):
        raise TocParseError("invalid", f"{label} extends beyond the table of contents")


def _entry_records_end(toc, entry_count, format_version):
    cursor = 0
    for index in range(entry_count):
        _require_range(toc, cursor, 2, f"entry {index} filename length")
        name_length = struct.unpack_from("<H", toc, cursor)[0]
        filename_size = name_length * 2 if format_version == "3.1" else name_length
        record_size = 2 + filename_size + PACK_ENTRY.size
        _require_range(toc, cursor, record_size, f"entry {index}")
        cursor += record_size
    return cursor


def _hash_seed_region(toc, hash_header_offset):
    _require_range(toc, hash_header_offset, HASH_HEADER.size, "hash header")
    signature_bytes, unknown1, unknown2, unknown3, payload_length = HASH_HEADER.unpack_from(
        toc,
        hash_header_offset,
    )
    signature = signature_bytes.rstrip(b"\x00").decode("ascii", errors="replace")
    if signature.startswith("HashVer1.4"):
        suffix_size = 0x48
        hash_version = "1.4"
    elif signature.startswith("HashVer1.3"):
        suffix_size = 0x24
        hash_version = "1.3"
    else:
        raise TocParseError("unsupported", f"unsupported QLIE hash header: {signature or 'empty'}")
    seed_offset = hash_header_offset + HASH_HEADER.size + payload_length + suffix_size
    _require_range(toc, seed_offset, HASH_SEED_BYTES, "hash seed region")
    metadata = {
        "unknown1": unknown1,
        "unknown2": unknown2,
        "unknown3": unknown3,
        "payload_length": payload_length,
    }
    return hash_version, toc[seed_offset : seed_offset + HASH_SEED_BYTES], metadata


def _parse_entries(toc, entry_count, seed, data_limit, format_version):
    cursor = 0
    entries = []
    warnings = []
    for index in range(entry_count):
        name_length = struct.unpack_from("<H", toc, cursor)[0]
        cursor += 2
        filename_size = name_length * 2 if format_version == "3.1" else name_length
        encoded_name = toc[cursor : cursor + filename_size]
        cursor += filename_size
        if format_version == "3.1":
            decoded_bytes = _decode_filename31(encoded_name, seed)
            codec = "utf-16-le"
        else:
            decoded_bytes = _decode_filename30(encoded_name, seed)
            codec = "cp932"
        try:
            internal_path = decoded_bytes.decode(codec)
        except UnicodeDecodeError as exc:
            internal_path = decoded_bytes.decode(codec, errors="replace")
            warnings.append(f"entry {index} filename is not valid {codec}: {exc}")

        fields = PACK_ENTRY.unpack_from(toc, cursor)
        cursor += PACK_ENTRY.size
        (
            offset,
            unknown1,
            stored_size,
            original_size,
            compression_flag,
            obfuscation_flag,
            entry_hash,
        ) = fields
        within_bounds = offset <= data_limit and stored_size <= data_limit - offset
        if not within_bounds:
            warnings.append(
                f"entry {index} data range [{offset}, {offset + stored_size}) exceeds TOC offset {data_limit}"
            )
        if not internal_path:
            warnings.append(f"entry {index} has an empty internal path")
        entries.append(
            ArchiveEntryInspection(
                index=index,
                internal_path=internal_path,
                filename_length_chars=len(internal_path),
                offset=offset,
                unknown1=unknown1,
                stored_size=stored_size,
                original_size=original_size,
                compression_flag=compression_flag,
                obfuscation_flag=obfuscation_flag,
                entry_hash=entry_hash,
                data_within_bounds=within_bounds,
            )
        )
    return tuple(entries), tuple(warnings)


def _parse_toc(toc, entry_count, data_limit, format_version):
    if entry_count > MAX_ENTRY_COUNT:
        raise TocParseError("invalid", f"entry count exceeds safety limit: {entry_count}")
    records_end = _entry_records_end(toc, entry_count, format_version)
    hash_version, hash_bytes, _hash_metadata = _hash_seed_region(toc, records_end)
    seed = (
        _filename_seed31(hash_bytes)
        if format_version == "3.1"
        else _filename_seed30(hash_bytes)
    )
    entries, warnings = _parse_entries(
        toc,
        entry_count,
        seed,
        data_limit,
        format_version,
    )
    return hash_version, seed, entries, warnings


def _parse_legacy_toc_variant(
    toc,
    entry_count,
    data_limit,
    *,
    include_length,
    include_hash,
):
    cursor = 0
    entries = []
    entry_struct = PACK_ENTRY if include_hash else PACK_ENTRY_WITHOUT_HASH
    for index in range(entry_count):
        _require_range(toc, cursor, 2, f"entry {index} filename length")
        name_length = struct.unpack_from("<H", toc, cursor)[0]
        cursor += 2
        if not 0 < name_length <= 0x100:
            raise TocParseError("invalid", f"entry {index} filename length is invalid")
        _require_range(toc, cursor, name_length + entry_struct.size, f"entry {index}")
        encoded_name = toc[cursor : cursor + name_length]
        cursor += name_length
        decoded_name = _decode_legacy_filename(
            encoded_name,
            include_length=include_length,
        )
        try:
            internal_path = decoded_name.decode("cp932")
        except UnicodeDecodeError as exc:
            raise TocParseError(
                "invalid",
                f"entry {index} filename is not valid cp932: {exc}",
            ) from exc
        fields = entry_struct.unpack_from(toc, cursor)
        cursor += entry_struct.size
        offset_low, offset_high, stored_size, original_size, compressed, encrypted = fields[:6]
        offset = offset_low | (offset_high << 32)
        entry_hash = fields[6] if include_hash else 0
        if offset > data_limit or stored_size > data_limit - offset:
            raise TocParseError("invalid", f"entry {index} payload is outside the archive data area")
        if compressed not in {0, 1}:
            raise TocParseError("invalid", f"entry {index} has an invalid compression flag")
        entries.append(
            ArchiveEntryInspection(
                index=index,
                internal_path=internal_path,
                filename_length_chars=len(internal_path),
                offset=offset,
                unknown1=offset_high,
                stored_size=stored_size,
                original_size=original_size,
                compression_flag=compressed,
                obfuscation_flag=encrypted,
                entry_hash=entry_hash,
                data_within_bounds=True,
            )
        )
    if cursor != len(toc):
        raise TocParseError("invalid", "legacy index layout does not consume the complete TOC")
    return tuple(entries)


def _parse_toc10(toc, entry_count, data_limit):
    variants = []
    for name, include_length, include_hash in (
        ("v1-without-hash", False, False),
        ("v2-without-hash", True, False),
        ("v2-with-hash", True, True),
    ):
        try:
            entries = _parse_legacy_toc_variant(
                toc,
                entry_count,
                data_limit,
                include_length=include_length,
                include_hash=include_hash,
            )
        except TocParseError:
            continue
        variants.append((name, entries))
    if not variants:
        raise TocParseError("unsupported", "FilePackVer1.0 index layout is not recognized")
    if len(variants) != 1:
        raise TocParseError("unsupported", "FilePackVer1.0 index layout is ambiguous")
    name, entries = variants[0]
    return name, entries


def _report(source_path, archive, status, reason, **kwargs):
    return QlieTocInspectionReport(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        source_path=str(source_path),
        status=status,
        reason=reason,
        archive=archive,
        **kwargs,
    )


def inspect_filepack_toc(path, hash_file=False):
    """Decode one supported QLIE FilePack TOC without reading payloads."""

    source_path = Path(path).expanduser().resolve()
    archive = inspect_archive(source_path, root=source_path.parent, hash_file=hash_file)
    if archive.status != "supported":
        return _report(source_path, archive, archive.status, archive.reason)
    if archive.format_version not in {"1.0", "3.0", "3.1"}:
        return _report(
            source_path,
            archive,
            "unsupported",
            "Phase 1 TOC decoding does not support this FilePack version",
        )
    if archive.toc_size_bytes is None or archive.toc_offset is None or archive.entry_count is None:
        return _report(source_path, archive, "invalid", "archive trailer is missing TOC metadata")
    if archive.toc_size_bytes > MAX_TOC_SIZE:
        return _report(
            source_path,
            archive,
            "invalid",
            f"TOC size exceeds safety limit: {archive.toc_size_bytes} bytes",
        )

    try:
        stat_before = source_path.stat()
        with source_path.open("rb") as stream:
            stream.seek(archive.toc_offset, os.SEEK_SET)
            toc = stream.read(archive.toc_size_bytes)
        stat_after = source_path.stat()
    except OSError as exc:
        return _report(source_path, archive, "unreadable", f"unable to read TOC: {exc}")
    if len(toc) != archive.toc_size_bytes:
        return _report(source_path, archive, "invalid", "TOC read ended before the declared trailer boundary")
    if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns):
        return _report(source_path, archive, "changed", "archive changed while its TOC was being inspected")

    try:
        if archive.format_version == "1.0":
            hash_version, entries = _parse_toc10(
                toc,
                archive.entry_count,
                archive.toc_offset,
            )
            seed = None
            warnings = ()
        else:
            hash_version, seed, entries, warnings = _parse_toc(
                toc,
                archive.entry_count,
                archive.toc_offset,
                archive.format_version,
            )
    except TocParseError as exc:
        return _report(source_path, archive, exc.status, exc.reason)

    status = "partial" if warnings else "supported"
    reason = f"decoded QLIE FilePackVer{archive.format_version} table of contents"
    if warnings:
        reason += " with warnings"
    return _report(
        source_path,
        archive,
        status,
        reason,
        hash_version=hash_version,
        obfuscation_seed=seed,
        entries=entries,
        warnings=warnings,
    )


def render_toc_report_text(report, limit=100):
    """Render a bounded human-readable view of a TOC report."""

    summary = report.to_dict()["summary"]
    lines = [
        f"QLIE TOC: {report.status}",
        f"archive: {report.source_path}",
        f"format: FilePackVer{report.archive.format_version or 'unknown'}",
        f"hash_version: {report.hash_version or 'unknown'}",
        f"seed: {report.obfuscation_seed if report.obfuscation_seed is not None else 'unknown'}",
        (
            "entries: "
            f"{summary['parsed_entry_count']}/{summary['declared_entry_count']} "
            f"(compressed={summary['compressed_entry_count']}, "
            f"obfuscated={summary['obfuscated_entry_count']}, "
            f"out_of_bounds={summary['out_of_bounds_entry_count']})"
        ),
    ]
    for entry in report.entries[:limit]:
        lines.append(
            f"[{entry.index}] {entry.internal_path} "
            f"offset={entry.offset} stored={entry.stored_size} original={entry.original_size} "
            f"compressed={entry.compression_flag} obfuscated={entry.obfuscation_flag} "
            f"hash={entry.entry_hash:08x}"
        )
    omitted = len(report.entries) - min(len(report.entries), limit)
    if omitted:
        lines.append(f"... {omitted} entries omitted; use --json for the complete TOC")
    if report.status not in {"supported", "partial"}:
        lines.append(f"reason: {report.reason}")
    lines.extend(f"warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)
