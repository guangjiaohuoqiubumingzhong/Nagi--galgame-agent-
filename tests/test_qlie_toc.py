import hashlib
import json
import struct

import pytest

from nagi.gameio.qlie.archive import (
    _filename_seed30,
    _filename_seed31,
    inspect_filepack_toc,
)


PACK_ENTRY = struct.Struct("<IIIIIII")
HASH_HEADER = struct.Struct("<16sIIII")
TRAILER = struct.Struct("<16sIII")


def test_filename_seed31_known_vector():
    assert _filename_seed31(bytes(range(256))) == 245891050


def test_filename_seed30_known_vector():
    fixture = bytes((index * 7 + 3) & 0xFF for index in range(256))
    assert _filename_seed30(fixture) == 25165824


def encode_filename(name, seed):
    raw = name.encode("utf-16-le")
    char_count = len(raw) // 2
    mutator = seed ^ ((seed >> 16) & 0xFFFF)
    key = ((char_count * char_count) ^ (char_count ^ 0x3E13) ^ mutator) & 0xFFFF
    current_key = key
    encoded = bytearray(len(raw))
    for index in range(char_count):
        current_key = ((current_key << 3) + index + key) & 0xFFFF
        word = struct.unpack_from("<H", raw, index * 2)[0] ^ current_key
        struct.pack_into("<H", encoded, index * 2, word)
    return struct.pack("<H", char_count) + encoded


def encode_filename30(name, seed):
    raw = name.encode("cp932")
    key = (len(raw) + (seed ^ 0x3E)) & 0xFF
    encoded = bytearray(raw)
    for index in range(1, len(encoded) + 1):
        encoded[index - 1] ^= (((index ^ key) & 0xFF) + index) & 0xFF
    return struct.pack("<H", len(raw)) + encoded


def encode_legacy_filename(name, *, include_length=True):
    raw = name.encode("cp932")
    key = 0xC4 ^ 0x3E
    if include_length:
        key += len(raw)
    encoded = bytearray(raw)
    for index in range(1, len(encoded) + 1):
        encoded[index - 1] ^= (((index ^ key) & 0xFF) + index) & 0xFF
    return struct.pack("<H", len(raw)) + encoded


def write_filepack31(path, entries, hash_version="1.3", hash_bytes=None):
    hash_bytes = hash_bytes or bytes(range(256))
    seed = _filename_seed31(hash_bytes)
    payload_size = max((entry[1] + entry[2] for entry in entries), default=0)
    payload = bytes(payload_size)
    records = bytearray()
    for index, (name, offset, stored_size, original_size, compressed, obfuscated) in enumerate(entries):
        records += encode_filename(name, seed)
        records += PACK_ENTRY.pack(
            offset,
            0x1000 + index,
            stored_size,
            original_size,
            compressed,
            obfuscated,
            0xA0000000 + index,
        )
    suffix_size = 0x48 if hash_version == "1.4" else 0x24
    hash_header = HASH_HEADER.pack(
        f"HashVer{hash_version}".encode("ascii"),
        1,
        2,
        3,
        0,
    )
    toc = bytes(records) + hash_header + bytes(suffix_size) + hash_bytes
    trailer = TRAILER.pack(b"FilePackVer3.1", len(entries), len(payload), 0)
    content = payload + toc + trailer
    path.write_bytes(content)
    return content, seed


def write_filepack30(path, entries, hash_version="1.3", hash_bytes=None):
    hash_bytes = hash_bytes or bytes(range(256))
    seed = _filename_seed30(hash_bytes)
    payload_size = max((entry[1] + entry[2] for entry in entries), default=0)
    payload = bytes(payload_size)
    records = bytearray()
    for index, (name, offset, stored_size, original_size, compressed, obfuscated) in enumerate(entries):
        records += encode_filename30(name, seed)
        records += PACK_ENTRY.pack(
            offset,
            0x1000 + index,
            stored_size,
            original_size,
            compressed,
            obfuscated,
            0xB0000000 + index,
        )
    suffix_size = 0x48 if hash_version == "1.4" else 0x24
    hash_header = HASH_HEADER.pack(
        f"HashVer{hash_version}".encode("ascii"), 1, 2, 3, 0
    )
    toc = bytes(records) + hash_header + bytes(suffix_size) + hash_bytes
    trailer = TRAILER.pack(b"FilePackVer3.0", len(entries), len(payload), 0)
    content = payload + toc + trailer
    path.write_bytes(content)
    return content, seed


def write_filepack10(path, name="scenario/main.s", payload=b"fixture"):
    record = encode_legacy_filename(name)
    record += PACK_ENTRY.pack(0, 0, len(payload), len(payload), 0, 0, 0x12345678)
    content = payload + record + TRAILER.pack(b"FilePackVer1.0", 1, len(payload), 0)
    path.write_bytes(content)
    return content


@pytest.mark.parametrize("hash_version", ["1.3", "1.4"])
def test_toc_decodes_unicode_paths_and_entry_metadata(tmp_path, hash_version):
    path = tmp_path / "data0.pack"
    _, expected_seed = write_filepack31(
        path,
        [
            ("scenario/第一章.s", 0, 4, 9, 1, 1),
            ("image/背景.png", 4, 3, 3, 0, 0),
        ],
        hash_version=hash_version,
    )

    report = inspect_filepack_toc(path)

    assert report.status == "supported"
    assert report.hash_version == hash_version
    assert report.obfuscation_seed == expected_seed
    assert [entry.internal_path for entry in report.entries] == [
        "scenario/第一章.s",
        "image/背景.png",
    ]
    assert report.entries[0].stored_size == 4
    assert report.entries[0].original_size == 9
    assert report.entries[0].compression_flag == 1
    assert report.entries[0].obfuscation_flag == 1
    assert report.entries[0].entry_hash == 0xA0000000
    assert all(entry.data_within_bounds for entry in report.entries)


def test_toc_json_is_stable_and_hashing_is_opt_in(tmp_path):
    path = tmp_path / "data0.pack"
    content, _ = write_filepack31(path, [("script.s", 0, 2, 2, 0, 0)])

    unhashed = inspect_filepack_toc(path)
    hashed = inspect_filepack_toc(path, hash_file=True)
    parsed = json.loads(hashed.to_json())

    assert unhashed.archive.sha256 is None
    assert hashed.archive.sha256 == hashlib.sha256(content).hexdigest()
    assert hashed.to_json() == hashed.to_json()
    assert parsed["summary"] == {
        "compressed_entry_count": 0,
        "declared_entry_count": 1,
        "obfuscated_entry_count": 0,
        "out_of_bounds_entry_count": 0,
        "parsed_entry_count": 1,
    }
    assert parsed["entries"][0]["entry_hash_hex"] == "a0000000"


def test_toc_inspection_is_read_only(tmp_path):
    path = tmp_path / "data0.pack"
    content, _ = write_filepack31(path, [("script.s", 0, 2, 2, 0, 0)])
    modified_before = path.stat().st_mtime_ns

    inspect_filepack_toc(path)

    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == modified_before


def test_toc_reports_out_of_bounds_entry_as_partial(tmp_path):
    path = tmp_path / "data0.pack"
    write_filepack31(path, [("bad.bin", 0, 4, 4, 0, 0)])
    data = bytearray(path.read_bytes())
    toc_offset = 4
    filename_size = 2 + len("bad.bin".encode("utf-16-le"))
    struct.pack_into("<I", data, toc_offset + filename_size + 8, 999)
    path.write_bytes(data)

    report = inspect_filepack_toc(path)

    assert report.status == "partial"
    assert report.entries[0].data_within_bounds is False
    assert "exceeds TOC offset" in report.warnings[0]


def test_toc_rejects_unknown_hash_header_and_truncation(tmp_path):
    unknown = tmp_path / "unknown.pack"
    write_filepack31(unknown, [])
    unknown_data = bytearray(unknown.read_bytes())
    struct.pack_into("<16s", unknown_data, 0, b"HashVer9.9")
    unknown.write_bytes(unknown_data)

    truncated = tmp_path / "truncated.pack"
    payload = b"x"
    toc = struct.pack("<H", 12) + b"too-short"
    truncated.write_bytes(payload + toc + TRAILER.pack(b"FilePackVer3.1", 1, 1, 0))

    unknown_report = inspect_filepack_toc(unknown)
    truncated_report = inspect_filepack_toc(truncated)

    assert unknown_report.status == "unsupported"
    assert "HashVer9.9" in unknown_report.reason
    assert truncated_report.status == "invalid"
    assert "extends beyond" in truncated_report.reason


def test_toc_decodes_filepack30_cp932_paths_and_entry_metadata(tmp_path):
    path = tmp_path / "old.pack"
    _, expected_seed = write_filepack30(
        path,
        [("scenario/第一章.s", 0, 4, 9, 1, 1)],
    )

    report = inspect_filepack_toc(path)

    assert report.status == "supported"
    assert report.obfuscation_seed == expected_seed
    assert report.entries[0].internal_path == "scenario/第一章.s"
    assert report.entries[0].stored_size == 4
    assert report.entries[0].original_size == 9
    assert report.entries[0].entry_hash == 0xB0000000


def test_toc_decodes_unambiguous_filepack10_v2_hash_layout(tmp_path):
    path = tmp_path / "patch.pack"
    write_filepack10(path, name="scenario/補丁.s")

    report = inspect_filepack_toc(path)

    assert report.status == "supported"
    assert report.hash_version == "v2-with-hash"
    assert report.obfuscation_seed is None
    assert report.entries[0].internal_path == "scenario/補丁.s"
    assert report.entries[0].entry_hash == 0x12345678
