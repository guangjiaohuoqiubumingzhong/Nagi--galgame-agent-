"""Bounded, in-memory reading of supported QLIE FilePack entries."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..models import ArchiveEntryInspection, QlieEntryProbeReport
from .archive import inspect_filepack_toc
from .detector import REPORT_SCHEMA_VERSION
from .pe import PeResourceError, load_icon_key_from_pe, load_reskey_from_pe


MAX_STORED_ENTRY_SIZE = 32 * 1024 * 1024
MAX_DECODED_ENTRY_SIZE = 64 * 1024 * 1024
DECODED_PREFIX_SIZE = 32
BPE_HEADER = struct.Struct("<4sB3sI")
MAX_KEY_FILE_SIZE = 4 * 1024 * 1024


class PayloadDecodeError(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class QlieEntryReadResult:
    report: QlieEntryProbeReport
    data: bytes | None = field(default=None, repr=False)


def _u32(value):
    return value & 0xFFFFFFFF


def _padd_lanes(left, right, bits, count):
    mask = (1 << bits) - 1
    result = 0
    for index in range(count):
        shift = index * bits
        lane = ((left >> shift) & mask) + ((right >> shift) & mask)
        result |= (lane & mask) << shift
    return result


def _paddb(left, right):
    return _padd_lanes(left, right, 8, 8)


def _paddw(left, right):
    return _padd_lanes(left, right, 16, 4)


def _paddd(left, right):
    return _padd_lanes(left, right, 32, 2)


def _pslld(value, amount):
    low = ((value & 0xFFFFFFFF) << amount) & 0xFFFFFFFF
    high = (((value >> 32) & 0xFFFFFFFF) << amount) & 0xFFFFFFFF
    return low | (high << 32)


def _build_u32_table(initial_key, constant):
    table = []
    eax = _u32(initial_key)
    for _ in range(64):
        eax ^= constant
        product = eax * constant
        eax = _u32((product & 0xFFFFFFFF) + (product >> 32))
        table.append(eax)
    return table


def _table_qword(table, byte_offset):
    index = byte_offset // 4
    return table[index] | (table[index + 1] << 32)


def _decode_mmx_blocks(data, table, box, key_index, normal_file):
    result = bytearray(data)
    key7 = _table_qword(table, 0x18)
    for offset in range(0, len(result) - len(result) % 8, 8):
        table_offset = ((key_index & 0xF) * 8) if normal_file else key_index
        key6 = _table_qword(table, table_offset)
        if normal_file:
            box_offset = (key_index & 0x7F) * 8
            key6 ^= int.from_bytes(box[box_offset : box_offset + 8], "little")
        key7 ^= key6
        key7 = _paddd(key7, key6)
        key1 = int.from_bytes(result[offset : offset + 8], "little") ^ key7
        result[offset : offset + 8] = key1.to_bytes(8, "little")
        key7 = _paddb(key7, key1)
        key7 ^= key1
        key7 = _pslld(key7, 1)
        key7 = _paddw(key7, key1)
        key_index = (key_index + (1 if normal_file else 8)) & 0x7F
    return bytes(result)


def _filename_keys(filename, initial_b, initial_s):
    key_b = initial_b
    key_s = initial_s
    raw_name = filename.encode("utf-16-le")
    for index in range(len(raw_name) // 2):
        code_unit = struct.unpack_from("<H", raw_name, index * 2)[0]
        key_b = _u32((code_unit << (index & 7)) + key_b)
        key_s = _u32(key_s ^ key_b)
    return key_b, key_s


def _unobfuscate_key_file(filename, data, seed):
    key_b, key_s = _filename_keys(filename, 0x85F532, 0x33F641)
    length = len(data)
    length_key = (length & 0x00FFFFFF) * 7
    key = _u32((0x8F32DC ^ length ^ key_b) + key_b + length + length_key)
    key = ((key ^ seed) + key_s) & 0x00FFFFFF
    key = _u32(key * 9)
    table = _build_u32_table(key, 0x8DF21431)
    key_index = (table[13] & 0x0F) * 8
    return _decode_mmx_blocks(data, table, b"", key_index, normal_file=False)


def _make_decode_table(resource_key):
    if len(resource_key) < 0x80:
        raise PayloadDecodeError("key_unavailable", "RCDATA/RESKEY is shorter than 128 bytes")
    table = bytearray(0x400)
    for index in range(0x400 // 4):
        value = (index + 3) * (index + 7)
        if index % 3:
            value = -value
        struct.pack_into("<I", table, index * 4, _u32(value))
    key = resource_key[0x31] % 0x49 + 0x80
    increment = resource_key[0x4F] % 7 + 7
    for index in range(len(table)):
        key += increment
        table[index] ^= resource_key[key % len(resource_key)]
    return bytes(table)


def _unobfuscate_normal_file(filename, data, seed, decode_table):
    key_b, key_s = _filename_keys(filename, 0x86F7E2, 0x4437F1)
    length = len(data)
    length_key = (length & 0x00FFFFFF) * 13
    key = _u32((0x56E213 ^ length ^ key_b) + key_b + length + length_key)
    key = ((key ^ seed) + key_s) & 0x00FFFFFF
    key = _u32(key * 13)
    table = _build_u32_table(key, 0x8A77F473)
    key_index = (table[8] & 0x0D) * 8
    return _decode_mmx_blocks(data, table, decode_table, key_index, normal_file=True)


class _QlieMersenneTwister:
    """QLIE's 64-word MT variant, reimplemented from GARbro's MIT source."""

    STATE_LENGTH = 64
    STATE_M = 39
    MATRIX_A = 0x9908B0DF
    SIGN_MASK = 0x80000000
    LOWER_MASK = 0x7FFFFFFF
    TEMPERING_MASK_B = 0x9C4F88E3
    TEMPERING_MASK_C = 0xE7F70000

    def __init__(self, seed):
        self.state = [0] * self.STATE_LENGTH
        self.state[0] = _u32(seed)
        for index in range(1, self.STATE_LENGTH):
            previous = self.state[index - 1]
            self.state[index] = _u32(
                0x6611BC19 * (previous ^ (previous >> 30)) + index
            )
        self.index = self.STATE_LENGTH

    def xor_state(self, key_data):
        for index in range(min(len(key_data) // 4, self.STATE_LENGTH)):
            self.state[index] ^= struct.unpack_from("<I", key_data, index * 4)[0]

    def rand(self):
        if self.index >= self.STATE_LENGTH:
            for index in range(self.STATE_LENGTH - self.STATE_M):
                following = self.state[index + 1]
                value = (self.state[index] & self.SIGN_MASK) | (
                    (following & self.LOWER_MASK) >> 1
                )
                self.state[index] = (
                    self.state[index + self.STATE_M]
                    ^ value
                    ^ (self.MATRIX_A if following & 1 else 0)
                )
            for index in range(
                self.STATE_LENGTH - self.STATE_M,
                self.STATE_LENGTH - 1,
            ):
                following = self.state[index + 1]
                value = (self.state[index] & self.SIGN_MASK) | (
                    (following & self.LOWER_MASK) >> 1
                )
                self.state[index] = (
                    self.state[index + self.STATE_M - self.STATE_LENGTH]
                    ^ value
                    ^ (self.MATRIX_A if following & 1 else 0)
                )
            following = self.state[0]
            value = (self.state[-1] & self.SIGN_MASK) | (
                (following & self.LOWER_MASK) >> 1
            )
            self.state[-1] = (
                self.state[self.STATE_M - 1]
                ^ value
                ^ (self.MATRIX_A if self.state[-2] & 1 else 0)
            )
            self.index = 0

        value = self.state[self.index]
        self.index += 1
        value ^= value >> 11
        value ^= (value << 7) & self.TEMPERING_MASK_B
        value ^= (value << 15) & self.TEMPERING_MASK_C
        value ^= value >> 18
        return _u32(value)

    def rand64(self):
        return self.rand() | (self.rand() << 32)


def _unobfuscate_file30(raw_filename, data, archive_seed, key_file, game_key):
    mutator = 0x85F532
    mt_seed = 0x33F641
    for index, value in enumerate(raw_filename):
        mutator = _u32(mutator + (index & 0xFF) * value)
        mt_seed = _u32(mt_seed ^ mutator)
    length = len(data)
    mt_seed = _u32(
        mt_seed
        + (
            archive_seed
            ^ (
                7 * (length & 0xFFFFFF)
                + length
                + mutator
                + (mutator ^ length ^ 0x8F32DC)
            )
        )
    )
    mt_seed = _u32(9 * (mt_seed & 0xFFFFFF))
    mt_seed ^= 0x453A

    generator = _QlieMersenneTwister(mt_seed)
    generator.xor_state(key_file)
    generator.xor_state(game_key)
    table = [generator.rand64() for _ in range(16)]
    for _ in range(9):
        generator.rand()
    rolling = generator.rand64()
    table_index = generator.rand() & 0x0F

    decoded = bytearray(data)
    for offset in range(0, len(decoded) - len(decoded) % 8, 8):
        table_value = table[table_index]
        rolling = _paddd(rolling ^ table_value, table_value)
        value = int.from_bytes(decoded[offset : offset + 8], "little") ^ rolling
        decoded[offset : offset + 8] = value.to_bytes(8, "little")
        rolling = _paddb(rolling, value) ^ value
        rolling = _paddw(_pslld(rolling, 1), value)
        table_index = (table_index + 1) & 0x0F
    return bytes(decoded)


def _read_key_file(path):
    source = Path(path).expanduser().resolve()
    try:
        stat_before = source.stat()
        if source.is_symlink() or not source.is_file():
            raise PayloadDecodeError("key_unavailable", "key.fkey is not a regular file")
        if stat_before.st_size > MAX_KEY_FILE_SIZE:
            raise PayloadDecodeError("invalid", "key.fkey exceeds safety limit")
        data = source.read_bytes()
        stat_after = source.stat()
    except PayloadDecodeError:
        raise
    except OSError as exc:
        raise PayloadDecodeError("key_unavailable", f"unable to read key.fkey: {exc}") from exc
    if (stat_before.st_size, stat_before.st_mtime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
    ):
        raise PayloadDecodeError("changed", "key.fkey changed while it was being read")
    if not data:
        raise PayloadDecodeError("key_unavailable", "key.fkey is empty")
    return data, str(source)


def _normalize_v30_key_material(value, label, *, maximum=MAX_KEY_FILE_SIZE):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadDecodeError("invalid", f"{label} must be bytes")
    data = bytes(value)
    if len(data) < 0x100:
        raise PayloadDecodeError("key_unavailable", f"{label} is shorter than 256 bytes")
    if len(data) > maximum:
        raise PayloadDecodeError("invalid", f"{label} exceeds safety limit")
    return data


def _find_key_file(archive_path):
    archive = Path(archive_path)
    candidates = (
        archive.parent / "key.fkey",
        archive.parent.parent / "key.fkey",
        archive.parent.parent / "DLL" / "key.fkey",
        archive.parent / "DLL" / "key.fkey",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _decode_v30_entry(entry, stored, archive_seed, key_file, game_key):
    decoded = stored
    if entry.obfuscation_flag:
        decoded = _unobfuscate_file30(
            entry.internal_path.encode("cp932"),
            decoded,
            archive_seed,
            key_file,
            game_key,
        )
    if entry.compression_flag:
        return _unbpe(decoded, entry.original_size), True
    if entry.original_size > len(decoded):
        raise PayloadDecodeError(
            "invalid",
            "uncompressed entry is shorter than its declared original size",
        )
    return decoded[: entry.original_size], False


def _read_byte(data, cursor, label):
    if cursor >= len(data):
        raise PayloadDecodeError("invalid", f"BPE {label} extends beyond compressed data")
    return data[cursor], cursor + 1


def _unbpe(data, expected_size):
    if len(data) < BPE_HEADER.size:
        raise PayloadDecodeError("invalid", "compressed entry is shorter than the BPE header")
    signature, flags, _unknown, header_size = BPE_HEADER.unpack_from(data)
    if signature != b"1PC\xff":
        raise PayloadDecodeError("invalid", "deobfuscated entry does not have a QLIE BPE signature")
    if header_size != expected_size:
        raise PayloadDecodeError(
            "invalid",
            f"BPE header size {header_size} does not match TOC size {expected_size}",
        )

    cursor = BPE_HEADER.size
    output = bytearray()
    while cursor < len(data):
        left = list(range(256))
        right = [0] * 256
        symbol = 0
        while symbol < 256:
            control, cursor = _read_byte(data, cursor, "literal table")
            if control > 127:
                literal_count = control - 127
                if symbol + literal_count > 256:
                    raise PayloadDecodeError("invalid", "BPE literal range exceeds 256 symbols")
                symbol += literal_count
                pair_count = 1
            else:
                pair_count = control + 1
            for _ in range(pair_count):
                if symbol >= 256:
                    break
                left_value, cursor = _read_byte(data, cursor, "pair table")
                left[symbol] = left_value
                if symbol != left_value:
                    right_value, cursor = _read_byte(data, cursor, "pair table")
                    right[symbol] = right_value
                symbol += 1

        length_size = 2 if flags & 0x01 else 4
        if cursor + length_size > len(data):
            raise PayloadDecodeError("invalid", "BPE block length extends beyond compressed data")
        block_size = int.from_bytes(data[cursor : cursor + length_size], "little")
        cursor += length_size
        block_end = cursor + block_size
        if block_end > len(data):
            raise PayloadDecodeError("invalid", "BPE block extends beyond compressed data")

        stack = []
        while cursor < block_end or stack:
            if stack:
                value = stack.pop()
            else:
                value = data[cursor]
                cursor += 1
            if value == left[value]:
                output.append(value)
                if len(output) > expected_size:
                    raise PayloadDecodeError("invalid", "BPE output exceeds declared size")
            else:
                if len(stack) >= 4095:
                    raise PayloadDecodeError("invalid", "BPE expansion stack exceeds safety limit")
                stack.append(right[value])
                stack.append(left[value])
        cursor = block_end

    if len(output) != expected_size:
        raise PayloadDecodeError(
            "invalid",
            f"BPE output size {len(output)} does not match declared size {expected_size}",
        )
    return bytes(output)


def _find_entry(toc_report, entry_index, internal_path):
    if (entry_index is None) == (internal_path is None):
        raise PayloadDecodeError("invalid_input", "select exactly one entry by index or internal path")
    if entry_index is not None:
        if entry_index < 0 or entry_index >= len(toc_report.entries):
            raise PayloadDecodeError("not_found", f"entry index is outside the TOC: {entry_index}")
        return toc_report.entries[entry_index], "index", str(entry_index)

    normalized = internal_path.replace("/", "\\").casefold()
    matches = [
        entry
        for entry in toc_report.entries
        if entry.internal_path.replace("/", "\\").casefold() == normalized
    ]
    if not matches:
        raise PayloadDecodeError("not_found", f"internal path is not present in the TOC: {internal_path}")
    if len(matches) > 1:
        raise PayloadDecodeError("ambiguous", f"internal path occurs {len(matches)} times: {internal_path}")
    return matches[0], "path", internal_path


def _make_report(source_path, selector_kind, selector_value, status, reason, **kwargs):
    return QlieEntryProbeReport(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        source_path=str(source_path),
        status=status,
        reason=reason,
        selector_kind=selector_kind,
        selector_value=str(selector_value),
        **kwargs,
    )


def read_filepack_entry(
    path,
    *,
    entry_index=None,
    internal_path=None,
    exe_path=None,
    resource_key=None,
    key_file_path=None,
    key_file=None,
    game_key=None,
    max_stored_size=MAX_STORED_ENTRY_SIZE,
    max_decoded_size=MAX_DECODED_ENTRY_SIZE,
):
    """Read and decode exactly one entry in memory; never write entry bytes."""

    source_path = Path(path).expanduser().resolve()
    selector_kind = "index" if entry_index is not None else "path"
    selector_value = entry_index if entry_index is not None else internal_path
    toc_report = inspect_filepack_toc(source_path)
    if toc_report.status not in {"supported", "partial"}:
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            toc_report.status,
            toc_report.reason,
            decode_stage="toc",
        )
        return QlieEntryReadResult(report)

    try:
        entry, selector_kind, selector_value = _find_entry(
            toc_report,
            entry_index,
            internal_path,
        )
        if not entry.data_within_bounds:
            raise PayloadDecodeError("invalid", "selected entry payload is outside the archive data area")
        if entry.stored_size > max_stored_size:
            raise PayloadDecodeError("limit_exceeded", "stored entry size exceeds probe safety limit")
        if entry.original_size > max_decoded_size:
            raise PayloadDecodeError("limit_exceeded", "decoded entry size exceeds probe safety limit")
        if entry.compression_flag not in {0, 1}:
            raise PayloadDecodeError(
                "unsupported",
                f"unsupported QLIE compression flag: {entry.compression_flag}",
            )
    except PayloadDecodeError as exc:
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            exc.status,
            exc.reason,
            decode_stage="selection",
        )
        return QlieEntryReadResult(report)

    try:
        stat_before = source_path.stat()
        with source_path.open("rb") as stream:
            stream.seek(entry.offset)
            stored = stream.read(entry.stored_size)
        stat_after = source_path.stat()
    except OSError as exc:
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            "unreadable",
            f"unable to read selected entry: {exc}",
            decode_stage="stored",
            entry=entry,
        )
        return QlieEntryReadResult(report)
    if len(stored) != entry.stored_size:
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            "invalid",
            "selected entry read ended before its declared length",
            decode_stage="stored",
            entry=entry,
        )
        return QlieEntryReadResult(report)
    if (stat_before.st_size, stat_before.st_mtime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
    ):
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            "changed",
            "archive changed while the selected entry was being read",
            decode_stage="stored",
            entry=entry,
        )
        return QlieEntryReadResult(report)

    stored_sha256 = hashlib.sha256(stored).hexdigest()
    seed = toc_report.obfuscation_seed
    if toc_report.archive.format_version in {"3.0", "3.1"} and seed is None:
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            "invalid",
            "TOC report is missing the QLIE obfuscation seed",
            decode_stage="stored",
            entry=entry,
            stored_sha256=stored_sha256,
            stored_size=len(stored),
        )
        return QlieEntryReadResult(report)

    key_source = None
    warnings = []
    try:
        if toc_report.archive.format_version == "1.0":
            if entry.obfuscation_flag:
                raise PayloadDecodeError(
                    "unsupported",
                    "encrypted FilePackVer1.0 payload variants are not yet supported",
                )
            if entry.compression_flag:
                decoded = _unbpe(stored, entry.original_size)
                compression_applied = True
            else:
                if entry.original_size > len(stored):
                    raise PayloadDecodeError(
                        "invalid",
                        "uncompressed entry is shorter than its declared original size",
                    )
                decoded = stored[: entry.original_size]
                compression_applied = False
            key_source = "not required"
        elif toc_report.archive.format_version == "3.0":
            effective_key_file = key_file
            key_file_source = "provided key.fkey bytes" if key_file is not None else None
            if entry.obfuscation_flag and effective_key_file is None:
                selected_key_path = (
                    Path(key_file_path).expanduser().resolve()
                    if key_file_path is not None
                    else _find_key_file(source_path)
                )
                if selected_key_path is None:
                    raise PayloadDecodeError(
                        "key_unavailable",
                        "FilePackVer3.0 entry requires key.fkey",
                    )
                effective_key_file, key_file_source = _read_key_file(selected_key_path)
            if entry.obfuscation_flag and game_key is None:
                if exe_path is None:
                    raise PayloadDecodeError(
                        "key_unavailable",
                        "FilePackVer3.0 entry requires IconKeyImage data from the game EXE",
                    )
                try:
                    game_key = load_icon_key_from_pe(exe_path)
                except PeResourceError as exc:
                    raise PayloadDecodeError(exc.status, exc.reason) from exc
            if entry.obfuscation_flag:
                effective_key_file = _normalize_v30_key_material(
                    effective_key_file,
                    "FilePackVer3.0 key.fkey",
                )
                game_key = _normalize_v30_key_material(
                    game_key,
                    "FilePackVer3.0 IconKeyImage",
                    maximum=0x100,
                )
                pack_key_entry = next(
                    (
                        item
                        for item in toc_report.entries
                        if "pack_keyfile" in item.internal_path.casefold()
                    ),
                    None,
                )
                if (
                    pack_key_entry is not None
                    and pack_key_entry.index != entry.index
                ):
                    if not pack_key_entry.data_within_bounds:
                        raise PayloadDecodeError(
                            "invalid",
                            "internal pack_keyfile payload is outside the archive data area",
                        )
                    if pack_key_entry.stored_size > MAX_KEY_FILE_SIZE:
                        raise PayloadDecodeError(
                            "limit_exceeded",
                            "internal pack_keyfile exceeds safety limit",
                        )
                    try:
                        with source_path.open("rb") as stream:
                            stream.seek(pack_key_entry.offset)
                            encoded_pack_key = stream.read(pack_key_entry.stored_size)
                        pack_key_stat_after = source_path.stat()
                    except OSError as exc:
                        raise PayloadDecodeError(
                            "unreadable",
                            f"unable to read internal pack_keyfile: {exc}",
                        ) from exc
                    if len(encoded_pack_key) != pack_key_entry.stored_size:
                        raise PayloadDecodeError(
                            "invalid",
                            "internal pack_keyfile read ended before its declared length",
                        )
                    if (stat_before.st_size, stat_before.st_mtime_ns) != (
                        pack_key_stat_after.st_size,
                        pack_key_stat_after.st_mtime_ns,
                    ):
                        raise PayloadDecodeError(
                            "changed",
                            "archive changed while the internal pack_keyfile was being read",
                        )
                    effective_key_file, _ = _decode_v30_entry(
                        pack_key_entry,
                        encoded_pack_key,
                        seed,
                        effective_key_file,
                        game_key,
                    )
                    key_file_source += " -> internal pack_keyfile"
                decoded, compression_applied = _decode_v30_entry(
                    entry,
                    stored,
                    seed,
                    effective_key_file,
                    game_key,
                )
                key_source = (
                    f"{key_file_source}; "
                    f"{Path(exe_path).expanduser().resolve()}#IconKeyImage"
                    if exe_path is not None
                    else f"{key_file_source}; provided IconKeyImage bytes"
                )
            else:
                if entry.compression_flag:
                    decoded = _unbpe(stored, entry.original_size)
                    compression_applied = True
                else:
                    if entry.original_size > len(stored):
                        raise PayloadDecodeError(
                            "invalid",
                            "uncompressed entry is shorter than its declared original size",
                        )
                    decoded = stored[: entry.original_size]
                    compression_applied = False
                key_source = "not required"
        elif "pack_keyfile" in entry.internal_path.casefold():
            decoded = _unobfuscate_key_file(entry.internal_path, stored, seed)
            if entry.compression_flag:
                warnings.append("QLIE keyfile branch does not apply BPE decompression")
            compression_applied = False
            key_source = "TOC seed (keyfile branch)"
        else:
            if resource_key is None:
                if exe_path is None:
                    report = _make_report(
                        source_path,
                        selector_kind,
                        selector_value,
                        "key_required",
                        "normal FilePackVer3.1 entries require RCDATA/RESKEY from the game EXE",
                        decode_stage="stored",
                        entry=entry,
                        stored_sha256=stored_sha256,
                        stored_size=len(stored),
                    )
                    return QlieEntryReadResult(report)
                try:
                    resource_key = load_reskey_from_pe(exe_path)
                except PeResourceError as exc:
                    report = _make_report(
                        source_path,
                        selector_kind,
                        selector_value,
                        exc.status,
                        exc.reason,
                        decode_stage="key",
                        entry=entry,
                        key_source=str(Path(exe_path).expanduser().resolve()),
                        stored_sha256=stored_sha256,
                        stored_size=len(stored),
                    )
                    return QlieEntryReadResult(report)
                key_source = str(Path(exe_path).expanduser().resolve()) + "#RCDATA/RESKEY"
            else:
                key_source = "provided RCDATA/RESKEY bytes"
            decode_table = _make_decode_table(resource_key)
            decoded = _unobfuscate_normal_file(
                entry.internal_path,
                stored,
                seed,
                decode_table,
            )
            if entry.compression_flag:
                decoded = _unbpe(decoded, entry.original_size)
                compression_applied = True
            else:
                if entry.original_size > len(decoded):
                    raise PayloadDecodeError(
                        "invalid",
                        "uncompressed entry is shorter than its declared original size",
                    )
                if entry.original_size != len(decoded):
                    warnings.append("uncompressed entry was truncated to its declared original size")
                decoded = decoded[: entry.original_size]
                compression_applied = False
    except PayloadDecodeError as exc:
        report = _make_report(
            source_path,
            selector_kind,
            selector_value,
            exc.status,
            exc.reason,
            decode_stage="decode",
            entry=entry,
            key_source=key_source,
            stored_sha256=stored_sha256,
            stored_size=len(stored),
            warnings=tuple(warnings),
        )
        return QlieEntryReadResult(report)

    decoded_sha256 = hashlib.sha256(decoded).hexdigest()
    report = _make_report(
        source_path,
        selector_kind,
        selector_value,
        "supported",
        f"decoded one QLIE FilePackVer{toc_report.archive.format_version} entry in memory",
        decode_stage="complete",
        entry=entry,
        key_source=key_source,
        stored_sha256=stored_sha256,
        decoded_sha256=decoded_sha256,
        stored_size=len(stored),
        decoded_size=len(decoded),
        decoded_prefix_hex=decoded[:DECODED_PREFIX_SIZE].hex(),
        compression_applied=compression_applied,
        warnings=tuple(warnings),
    )
    return QlieEntryReadResult(report, decoded)


def render_probe_report_text(report):
    lines = [
        f"QLIE entry probe: {report.status}",
        f"archive: {report.source_path}",
        f"selector: {report.selector_kind}={report.selector_value}",
        f"stage: {report.decode_stage}",
    ]
    if report.entry:
        lines.extend(
            [
                f"entry: [{report.entry.index}] {report.entry.internal_path}",
                f"stored_size: {report.stored_size if report.stored_size is not None else report.entry.stored_size}",
                f"original_size: {report.entry.original_size}",
                f"flags: compressed={report.entry.compression_flag} obfuscated={report.entry.obfuscation_flag}",
            ]
        )
    if report.key_source:
        lines.append(f"key_source: {report.key_source}")
    if report.stored_sha256:
        lines.append(f"stored_sha256: {report.stored_sha256}")
    if report.decoded_sha256:
        lines.append(f"decoded_sha256: {report.decoded_sha256}")
    if report.decoded_size is not None:
        lines.append(f"decoded_size: {report.decoded_size}")
    if report.decoded_prefix_hex is not None:
        lines.append(f"decoded_prefix_hex: {report.decoded_prefix_hex}")
    lines.append(f"reason: {report.reason}")
    lines.extend(f"warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)
