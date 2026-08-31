import hashlib
import json
from pathlib import Path

import pytest

from nagi.gameio.qlie.script import (
    parse_exported_script,
    parse_qlie_script_bytes,
    scan_qlie_script_lines,
    survey_script_export,
)
from scripts.evaluate_qlie_parser import evaluate


def write_export_workspace(root, scripts):
    root.mkdir()
    items = []
    for index, (output_path, internal_path, data) in enumerate(scripts):
        target = root.joinpath(*output_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        items.append(
            {
                "archive_name": "GameData/data6.pack",
                "archive_path": "synthetic/data6.pack",
                "archive_rank": 0,
                "compression_flag": 1,
                "conflict_group": None,
                "decoded_sha256": hashlib.sha256(data).hexdigest(),
                "decoded_size": len(data),
                "entry_hash": index,
                "entry_hash_hex": f"{index:08x}",
                "entry_index": index,
                "internal_path": internal_path,
                "obfuscation_flag": 2,
                "original_size": len(data),
                "output_path": output_path,
                "reason": "synthetic fixture",
                "shadowed_by": None,
                "status": "exported",
                "stored_sha256": hashlib.sha256(data).hexdigest(),
                "stored_size": len(data),
            }
        )
    manifest = {
        "schema_version": 1,
        "engine": "qlie",
        "extensions": [".s", ".txt"],
        "items": items,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def utf16le_bom(text):
    return b"\xff\xfe" + text.encode("utf-16-le")


def test_survey_reports_structure_without_emitting_script_text(tmp_path):
    export_dir = tmp_path / "export"
    secret_ascii = "PrivateSpeakerName"
    secret_non_ascii = "秘密の台詞"
    script = utf16le_bom(
        "@@scene_01\r\n"
        f"\\message speaker={secret_ascii}\r\n"
        f"{secret_non_ascii}\r\n"
        "; comment\r\n"
    )
    write_export_workspace(
        export_dir,
        [("resolved/scenario/main.s", "scenario\\main.s", script)],
    )
    script_path = export_dir / "resolved" / "scenario" / "main.s"
    modified_before = script_path.stat().st_mtime_ns

    report = survey_script_export(export_dir, top_shapes=20)
    payload = report.to_dict()
    serialized = report.to_json()

    assert report.status == "supported"
    assert payload["summary"]["surveyed_count"] == 1
    assert payload["summary"]["archive_counts"] == {"GameData/data6.pack": 1}
    assert payload["summary"]["extension_counts"] == {".s": 1}
    assert payload["summary"]["conflict_variant_count"] == 0
    assert payload["summary"]["encoding_counts"] == {"utf-16-le-bom": 1}
    assert payload["summary"]["newline_style_counts"] == {"crlf": 1}
    assert payload["summary"]["line_family_counts"] == {
        "at_label": 1,
        "backslash_command": 1,
        "comment_like": 1,
        "non_ascii_text_candidate": 1,
    }
    assert payload["files"][0]["line_count"] == 4
    assert secret_ascii not in serialized
    assert secret_non_ascii not in serialized
    assert report.to_json() == serialized
    assert script_path.stat().st_mtime_ns == modified_before


def test_survey_supports_utf8_and_empty_scripts(tmp_path):
    export_dir = tmp_path / "export"
    write_export_workspace(
        export_dir,
        [
            ("resolved/config.txt", "config.txt", b"name=value\n"),
            ("resolved/empty.txt", "empty.txt", b""),
        ],
    )

    report = survey_script_export(export_dir)

    assert report.status == "supported"
    assert report.to_dict()["summary"]["encoding_counts"] == {
        "empty": 1,
        "utf-8": 1,
    }


def test_survey_rejects_modified_script_before_decoding(tmp_path):
    export_dir = tmp_path / "export"
    original = utf16le_bom("original text\r\n")
    write_export_workspace(
        export_dir,
        [("resolved/main.s", "main.s", original)],
    )
    script_path = export_dir / "resolved" / "main.s"
    script_path.write_bytes(utf16le_bom("modified text\r\n"))

    report = survey_script_export(export_dir)

    assert report.status == "partial"
    assert report.files[0].status in {"size_mismatch", "hash_mismatch"}
    assert report.syntax_shapes == ()


def test_survey_rejects_manifest_path_traversal(tmp_path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    data = b"outside"
    manifest = {
        "items": [
            {
                "status": "exported",
                "output_path": "../outside.s",
                "archive_name": "data.pack",
                "internal_path": "outside.s",
                "conflict_group": None,
                "decoded_size": len(data),
                "decoded_sha256": hashlib.sha256(data).hexdigest(),
            }
        ]
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = survey_script_export(export_dir)

    assert report.status == "invalid_input"
    assert "traversal" in report.reason


def test_survey_missing_manifest_is_structured_error(tmp_path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    report = survey_script_export(export_dir)

    assert report.status == "invalid_input"
    assert "manifest.json" in report.reason


@pytest.mark.parametrize(
    ("data", "encoding", "codec", "bom_size"),
    [
        (
            b"\xff\xfe" + "一\r\n二\n三\r四".encode("utf-16-le"),
            "utf-16-le-bom",
            "utf-16-le",
            2,
        ),
        (
            b"\xfe\xff" + "一\r\n二\n三\r四".encode("utf-16-be"),
            "utf-16-be-bom",
            "utf-16-be",
            2,
        ),
        (
            b"\xef\xbb\xbf" + "一\r\n二\n三\r四".encode("utf-8"),
            "utf-8-bom",
            "utf-8",
            3,
        ),
        (
            "一\r\n二\n三\r四".encode("cp932"),
            "cp932",
            "cp932",
            0,
        ),
    ],
)
def test_line_scanner_preserves_exact_offsets_for_supported_encodings(
    data,
    encoding,
    codec,
    bom_size,
):
    scan = scan_qlie_script_lines(data)

    assert scan.encoding == encoding
    assert scan.bom_size == bom_size
    assert [line.newline for line in scan.lines] == ["crlf", "lf", "cr", "none"]
    assert [line.number for line in scan.lines] == [1, 2, 3, 4]
    for line in scan.lines:
        assert data[line.byte_start : line.byte_end].decode(codec) == line.text
    rebuilt = data[:bom_size] + b"".join(
        data[line.byte_start : line.newline_byte_end] for line in scan.lines
    )
    assert rebuilt == data


def test_parser_classifies_known_qlie_forms_and_preserves_unknown_lines():
    text = (
        "@@intro\r\n"
        "〖Alice〗\r\n"
        "「Hello {name}!」\r\n"
        '^select,"Go [fast]","Stay"\r\n'
        "^savedate,セーブ一\r\n"
        "\\jmp,@@end\r\n"
        "plain ascii text\r\n"
    )
    data = b"\xff\xfe" + text.encode("utf-16-le")

    first = parse_qlie_script_bytes(
        data,
        archive_name="GameData/data6.pack",
        internal_path="scenario\\fixture.s",
        output_path="resolved/scenario/fixture.s",
        entry_index=9,
    )
    second = parse_qlie_script_bytes(
        data,
        archive_name="GameData/data6.pack",
        internal_path="scenario\\fixture.s",
        output_path="resolved/scenario/fixture.s",
        entry_index=9,
    )

    assert first.status == "supported"
    assert first.to_jsonl() == second.to_jsonl()
    assert first.to_dict()["summary"]["kind_counts"] == {
        "choice": 2,
        "control": 1,
        "dialogue": 1,
        "label": 1,
        "metadata": 1,
        "speaker_name": 1,
        "unknown": 1,
    }
    dialogue = next(segment for segment in first.segments if segment.kind == "dialogue")
    choices = [segment for segment in first.segments if segment.kind == "choice"]
    unknown = next(segment for segment in first.segments if segment.kind == "unknown")
    assert dialogue.speaker == "Alice"
    assert dialogue.placeholders[0].raw == "{name}"
    assert choices[0].tags[0].raw == "[fast]"
    assert all(segment.translatable for segment in choices)
    assert not unknown.translatable
    assert unknown.warnings == ("unrecognized QLIE line structure",)
    for segment in first.segments:
        source = segment.source
        assert data[source.byte_start : source.byte_end].decode("utf-16-le") == segment.source_text


def test_parser_handles_pc_ruby_voice_metadata_and_structural_assignments():
    data = (
        "[pc,Poetic caption]\n"
        "[pc,Ending punctuation]、\n"
        "[rb,日課,・・]に取り組む。\n"
        "act[0]=夕摩,覡夕摩\n"
        "Font[0].List[2]=\n"
        "$bottom=720\n"
        "[BltModeDialog]\n"
    ).encode("utf-8")

    result = parse_qlie_script_bytes(
        data,
        archive_name="GameData/data6.pack",
        internal_path="scenario\\special-forms.s",
        output_path="resolved/scenario/special-forms.s",
        entry_index=10,
    )

    assert result.status == "supported"
    assert result.to_dict()["summary"]["kind_counts"] == {
        "control": 3,
        "metadata": 1,
        "narration": 4,
    }
    assert result.to_dict()["summary"]["translatable_count"] == 5
    assert [
        segment.source_text for segment in result.segments if segment.kind == "narration"
    ] == ["Poetic caption", "Ending punctuation", "、", "[rb,日課,・・]に取り組む。"]
    ruby = next(
        segment
        for segment in result.segments
        if segment.source_text.startswith("[rb,")
    )
    metadata = next(segment for segment in result.segments if segment.kind == "metadata")
    assert ruby.tags[0].raw == "[rb,日課,・・]"
    assert metadata.source_text == "夕摩,覡夕摩"
    for segment in result.segments:
        source = segment.source
        assert data[source.byte_start : source.byte_end].decode("utf-8") == segment.source_text


def test_parser_output_matches_synthetic_golden():
    input_path = Path("tests/fixtures/qlie_synthetic/parser-input-utf8.s")
    golden_path = Path("tests/fixtures/qlie_synthetic/parser-output-v1.jsonl")

    result = parse_qlie_script_bytes(
        input_path.read_bytes(),
        archive_name="GameData/data6.pack",
        internal_path="scenario\\parser-fixture.s",
        output_path="resolved/scenario/parser-fixture.s",
        entry_index=7,
    )

    assert result.status == "supported"
    assert result.to_jsonl() == golden_path.read_text(encoding="utf-8")


def test_parse_exported_script_validates_manifest_hash(tmp_path):
    export_dir = tmp_path / "export"
    data = "「fixture」\r\n".encode("utf-8")
    write_export_workspace(
        export_dir,
        [("resolved/main.s", "main.s", data)],
    )

    supported = parse_exported_script(export_dir, "resolved/main.s")
    (export_dir / "resolved" / "main.s").write_bytes(b"modified")
    modified = parse_exported_script(export_dir, "resolved/main.s")

    assert supported.status == "supported"
    assert supported.segments[0].kind == "dialogue"
    assert modified.status in {"size_mismatch", "hash_mismatch"}


def test_parser_rejects_invalid_identity_and_encoding():
    unsafe = parse_qlie_script_bytes(
        b"text",
        archive_name="GameData/data.pack",
        internal_path="..\\outside.s",
        output_path="resolved/main.s",
        entry_index=0,
    )
    invalid_encoding = parse_qlie_script_bytes(
        b"\x81",
        archive_name="GameData/data.pack",
        internal_path="main.s",
        output_path="resolved/main.s",
        entry_index=0,
    )

    assert unsafe.status == "invalid_input"
    assert invalid_encoding.status == "unsupported_encoding"


def test_parser_evaluation_report_is_content_free(tmp_path):
    export_dir = tmp_path / "export"
    secret_text = "「DoNotEmitEvaluationText」\r\n"
    data = secret_text.encode("utf-8")
    write_export_workspace(
        export_dir,
        [("resolved/main.s", "main.s", data)],
    )

    report = evaluate(export_dir)
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["status"] == "supported"
    assert report["summary"]["translatable_count"] == 1
    assert secret_text.strip() not in serialized
