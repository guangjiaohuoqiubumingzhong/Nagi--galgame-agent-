"""Rebuild FilePack 3.1 scripts without changing names, ordering or other assets.

The original index's auxiliary hash/name tables and seed are retained. Changed
payloads are appended before the index; untouched encrypted entries stay byte
for byte identical. Only a separate, newly created output file is accepted.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from .archive import (
    PACK_ENTRY,
    _add_u16_lanes,
    _rotate_left_32_lanes,
    _signed_u16,
    inspect_filepack_toc,
)
from .detector import FILEPACK_TRAILER
from .payload import (
    MAX_DECODED_ENTRY_SIZE,
    _build_u32_table,
    _filename_keys,
    _make_decode_table,
    _paddb,
    _paddd,
    _paddw,
    _pslld,
    _table_qword,
    _u32,
    read_filepack_entry,
)
from .pe import load_reskey_from_pe


def hash31(data):
    """FilePack 3.1's 32-bit payload checksum (not a cryptographic digest)."""
    key = value = 0
    for offset in range(0, len(data) - len(data) % 8, 8):
        key = _add_u16_lanes(key, 0xA35793A7A35793A7)
        value = _add_u16_lanes(
            value, int.from_bytes(data[offset : offset + 8], "little") ^ key
        )
        value = _rotate_left_32_lanes(value, 3)
    words = [_signed_u16((value >> shift) & 0xFFFF) for shift in (0, 16, 32, 48)]
    return (words[0] * words[2] + words[1] * words[3]) & 0xFFFFFFFF


def encrypt31(filename, plaintext, seed, resource_key):
    """Inverse of the existing RESKEY-backed method-2 decoder."""
    key_b, key_s = _filename_keys(filename, 0x86F7E2, 0x4437F1)
    length = len(plaintext)
    key = _u32((0x56E213 ^ length ^ key_b) + key_b + length + (length & 0xFFFFFF) * 13)
    key = ((key ^ seed) + key_s) & 0xFFFFFF
    table = _build_u32_table(_u32(key * 13), 0x8A77F473)
    box = _make_decode_table(resource_key)
    index = (table[8] & 0x0D) * 8
    state = _table_qword(table, 0x18)
    result = bytearray(plaintext)
    for offset in range(0, length - length % 8, 8):
        component = _table_qword(table, (index & 0xF) * 8)
        component ^= int.from_bytes(
            box[(index & 0x7F) * 8 : (index & 0x7F) * 8 + 8], "little"
        )
        state = _paddd(state ^ component, component)
        plain = int.from_bytes(plaintext[offset : offset + 8], "little")
        result[offset : offset + 8] = (plain ^ state).to_bytes(8, "little")
        state = _paddw(_pslld(_paddb(state, plain) ^ plain, 1), plain)
        index = (index + 1) & 0x7F
    return bytes(result)


def repack_scripts(source, target, replacements, *, exe_path=None, resource_key=None):
    """Replace {entry_index: bytes}, then decrypt every change to verify it.

    No arbitrary new entries, executable payloads, in-place writes or unsupported
    FilePack variants are accepted. Failure never leaves a publishable archive.
    """
    source, target = Path(source), Path(target)
    if (
        source.is_symlink()
        or target.is_symlink()
        or target.exists()
        or source.resolve() == target.resolve()
    ):
        raise ValueError("QLIE 回包必须写入全新的独立文件")
    report = inspect_filepack_toc(source)
    if report.status != "supported" or report.archive.format_version != "3.1":
        raise ValueError("QLIE 回包目前仅支持已验证的 FilePackVer3.1 格式")
    if not replacements or len(replacements) > 5000:
        raise ValueError("QLIE 回包没有有效的译文脚本")
    if resource_key is None:
        if exe_path is None:
            raise ValueError("QLIE 回包需要原游戏的 RESKEY")
        resource_key = load_reskey_from_pe(exe_path)
    before = source.stat()
    with source.open("rb") as stream:
        stream.seek(report.archive.toc_offset)
        toc = bytearray(stream.read(report.archive.toc_size_bytes))
        trailer = stream.read(FILEPACK_TRAILER.size)
    signature, count, old_offset, reserved = FILEPACK_TRAILER.unpack(trailer)
    cursor = 0
    fields = {}
    for entry in report.entries:
        name_length = struct.unpack_from("<H", toc, cursor)[0]
        cursor += 2 + name_length * 2
        fields[entry.index] = cursor
        cursor += PACK_ENTRY.size
    encoded = {}
    for index, data in replacements.items():
        if type(index) is not int or index not in fields:
            raise ValueError("QLIE 译文指向不存在的脚本条目")
        entry = report.entries[index]
        if (
            Path(entry.internal_path).suffix.lower() not in {".s", ".txt"}
            or "pack_keyfile" in entry.internal_path.casefold()
            or entry.obfuscation_flag != 2
            or entry.unknown1 != 0
        ):
            raise ValueError("QLIE 仅允许替换已适配的 method-2 文字脚本")
        if (
            not isinstance(data, bytes)
            or not data
            or len(data) > MAX_DECODED_ENTRY_SIZE
        ):
            raise ValueError("QLIE 译文脚本大小无效")
        encoded[index] = encrypt31(
            entry.internal_path, data, report.obfuscation_seed, resource_key
        )
    new_offset = old_offset + sum(map(len, encoded.values()))
    if new_offset > 0xFFFFFFFF:
        raise ValueError("QLIE 回包超过当前格式的大小限制")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as src, target.open("xb") as dst:
            created = True
            remaining = old_offset
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("QLIE 原包在复制时被截断")
                dst.write(block)
                remaining -= len(block)
            for index in sorted(encoded):
                data = encoded[index]
                PACK_ENTRY.pack_into(
                    toc,
                    fields[index],
                    dst.tell(),
                    0,
                    len(data),
                    len(replacements[index]),
                    0,
                    2,
                    hash31(data),
                )
                dst.write(data)
            dst.write(toc)
            dst.write(FILEPACK_TRAILER.pack(signature, count, new_offset, reserved))
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("QLIE 原包在回包过程中发生变化")
        verified = inspect_filepack_toc(target)
        if verified.status != "supported" or len(verified.entries) != len(
            report.entries
        ):
            raise ValueError("QLIE 回包后的目录校验失败")
        for index, expected in replacements.items():
            actual = read_filepack_entry(
                target, entry_index=index, resource_key=resource_key
            )
            if actual.data != expected:
                raise ValueError("QLIE 回包后的译文校验失败")
        return {
            "format": "3.1",
            "changed_entries": len(replacements),
            "sha256": _digest(target),
        }
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
