import hashlib
import json
import struct

import pytest

from nagi.gameio.qlie.detector import inspect_archive, inspect_game_directory


def write_synthetic_pack(path, version="3.1", entry_count=2, payload=b"payload", toc=b"toc"):
    signature = f"FilePackVer{version}".encode("ascii") + b"\x00"
    trailer = struct.pack("<16sIII", signature, entry_count, len(payload), 0)
    content = payload + toc + trailer
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_inspect_archive_recognizes_supported_filepack_versions(tmp_path, version):
    path = tmp_path / f"data-{version}.pack"
    content = write_synthetic_pack(path, version=version, entry_count=7, toc=b"table")

    result = inspect_archive(path, root=tmp_path)

    assert result.status == "supported"
    assert result.format_version == version
    assert result.signature == f"FilePackVer{version}"
    assert result.entry_count == 7
    assert result.toc_offset == len(b"payload")
    assert result.toc_size_bytes == len(b"table")
    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()


def test_inspect_archive_returns_structured_results_for_unknown_short_and_missing_files(tmp_path):
    unknown = tmp_path / "unknown.pack"
    unknown.write_bytes(b"payload" + struct.pack("<16sIII", b"OtherFormat1.0", 1, 0, 0))
    short = tmp_path / "short.pack"
    short.write_bytes(b"too short")

    unknown_result = inspect_archive(unknown, root=tmp_path)
    short_result = inspect_archive(short, root=tmp_path)
    missing_result = inspect_archive(tmp_path / "missing.pack", root=tmp_path)

    assert unknown_result.status == "unsupported"
    assert unknown_result.signature == "OtherFormat1.0"
    assert short_result.status == "invalid"
    assert short_result.sha256 == hashlib.sha256(b"too short").hexdigest()
    assert missing_result.status == "unreadable"
    assert "unable to read archive" in missing_result.reason


def test_inspect_archive_rejects_toc_offset_beyond_trailer(tmp_path):
    path = tmp_path / "broken.pack"
    payload = b"tiny"
    signature = b"FilePackVer3.1\x00"
    path.write_bytes(payload + struct.pack("<16sIII", signature, 1, 999_999, 0))

    result = inspect_archive(path, root=tmp_path)

    assert result.status == "invalid"
    assert "offset" in result.reason


def test_directory_inspection_is_sorted_stable_and_read_only(tmp_path):
    first_path = tmp_path / "A.pack"
    second_path = tmp_path / "nested" / "b.PACK"
    first_content = write_synthetic_pack(first_path, version="3.1")
    second_content = write_synthetic_pack(second_path, version="3.0")
    before = {
        first_path: (first_path.read_bytes(), first_path.stat().st_mtime_ns),
        second_path: (second_path.read_bytes(), second_path.stat().st_mtime_ns),
    }

    report = inspect_game_directory(tmp_path)
    serialized_once = report.to_json()
    serialized_twice = report.to_json()

    assert report.status == "supported"
    assert [archive.relative_path for archive in report.archives] == ["A.pack", "nested/b.PACK"]
    assert json.loads(serialized_once)["summary"] == {
        "archive_count": 2,
        "status_counts": {"supported": 2},
    }
    assert serialized_once == serialized_twice
    assert first_path.read_bytes() == first_content == before[first_path][0]
    assert second_path.read_bytes() == second_content == before[second_path][0]
    assert first_path.stat().st_mtime_ns == before[first_path][1]
    assert second_path.stat().st_mtime_ns == before[second_path][1]


def test_directory_inspection_reports_partial_and_can_skip_hashing(tmp_path):
    write_synthetic_pack(tmp_path / "good.pack")
    (tmp_path / "bad.pack").write_bytes(b"bad")

    report = inspect_game_directory(tmp_path, hash_files=False)

    assert report.status == "partial"
    assert {archive.status for archive in report.archives} == {"supported", "invalid"}
    assert all(archive.sha256 is None for archive in report.archives)


def test_directory_inspection_rejects_invalid_root(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        inspect_game_directory(tmp_path / "missing")

    file_path = tmp_path / "game.exe"
    file_path.write_bytes(b"exe")
    with pytest.raises(ValueError, match="not a directory"):
        inspect_game_directory(file_path)
