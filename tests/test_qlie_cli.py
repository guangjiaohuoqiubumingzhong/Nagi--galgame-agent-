import json
import hashlib
import struct

from nagi import cli
from nagi.gameio.qlie.archive import _filename_seed31
from test_qlie_payload import (
    encrypt_normal_file,
    make_literal_bpe,
    write_payload_pack,
    write_synthetic_pe,
)
from test_qlie_toc import write_filepack31


def write_synthetic_pack(path):
    payload = b"payload"
    toc = b"toc"
    trailer = struct.pack("<16sIII", b"FilePackVer3.1\x00", 3, len(payload), 0)
    path.write_bytes(payload + toc + trailer)


def test_qlie_inspect_json_does_not_build_agent(tmp_path, monkeypatch, capsys):
    write_synthetic_pack(tmp_path / "data0.pack")

    def fail_if_called(_args):
        raise AssertionError("QLIE inspection must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    exit_code = cli.main(["qlie", "inspect", str(tmp_path), "--json", "--skip-hash"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["engine"] == "qlie"
    assert payload["status"] == "supported"
    assert payload["archives"][0]["format_version"] == "3.1"
    assert payload["archives"][0]["sha256"] is None


def test_qlie_inspect_human_output_is_compact(tmp_path, capsys):
    write_synthetic_pack(tmp_path / "data0.pack")

    exit_code = cli.main(["qlie", "inspect", str(tmp_path), "--skip-hash"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "QLIE inspection: supported" in captured.out
    assert "data0.pack: supported, FilePackVer3.1" in captured.out
    assert "sha256=skipped" in captured.out


def test_qlie_inspect_invalid_directory_returns_structured_json_error(tmp_path, capsys):
    exit_code = cli.main(["qlie", "inspect", str(tmp_path / "missing"), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["status"] == "invalid_input"
    assert "does not exist" in payload["error"]


def test_qlie_list_json_does_not_build_agent(tmp_path, monkeypatch, capsys):
    path = tmp_path / "data0.pack"
    write_filepack31(path, [("scenario/main.s", 0, 1, 1, 0, 1)])

    def fail_if_called(_args):
        raise AssertionError("QLIE TOC listing must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    exit_code = cli.main(["qlie", "list", str(path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["status"] == "supported"
    assert payload["entries"][0]["internal_path"] == "scenario/main.s"
    assert payload["archive"]["sha256"] is None


def test_qlie_list_human_output_honors_limit(tmp_path, capsys):
    path = tmp_path / "data0.pack"
    write_filepack31(
        path,
        [
            ("first.s", 0, 1, 1, 0, 0),
            ("second.s", 1, 1, 1, 0, 0),
        ],
    )

    exit_code = cli.main(["qlie", "list", str(path), "--limit", "1"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "first.s" in captured.out
    assert "second.s" not in captured.out
    assert "1 entries omitted" in captured.out


def test_qlie_list_invalid_limit_returns_error(tmp_path, capsys):
    path = tmp_path / "data0.pack"
    write_filepack31(path, [])

    exit_code = cli.main(["qlie", "list", str(path), "--limit", "-1"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--limit must be zero or greater" in captured.err


def test_qlie_probe_json_does_not_build_agent_or_emit_payload(tmp_path, monkeypatch, capsys):
    path = tmp_path / "data.pack"
    write_payload_pack(path, "scenario\\main.s", b"encrypted", 9, 0)

    def fail_if_called(_args):
        raise AssertionError("QLIE entry probing must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    exit_code = cli.main(
        ["qlie", "probe", str(path), "--path", "scenario/main.s", "--json"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert payload["status"] == "key_required"
    assert payload["entry"]["internal_path"] == "scenario\\main.s"
    assert "data" not in payload
    assert "encrypted" not in captured.out


def test_qlie_export_plan_json_does_not_build_agent(tmp_path, monkeypatch, capsys):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    write_filepack31(
        game_dir / "data.pack",
        [("scenario\\main.s", 0, 1, 1, 0, 2)],
    )

    def fail_if_called(_args):
        raise AssertionError("QLIE export planning must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    exit_code = cli.main(
        [
            "qlie",
            "export-plan",
            str(game_dir),
            "--output",
            str(tmp_path / "export"),
            "--archive",
            "data.pack",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["status"] == "ready"
    assert payload["summary"]["selected_count"] == 1
    assert not (tmp_path / "export").exists()


def test_qlie_export_scripts_requires_apply_and_exports_transactionally(
    tmp_path,
    monkeypatch,
    capsys,
):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pack_path = game_dir / "data.pack"
    exe_path = game_dir / "game.exe"
    output_dir = tmp_path / "export"
    name = "scenario\\main.s"
    plaintext = b"synthetic CLI fixture"
    resource_key = bytes(range(256))
    seed = _filename_seed31(bytes(range(256)))
    encrypted = encrypt_normal_file(
        name,
        make_literal_bpe(plaintext),
        seed,
        resource_key,
    )
    write_payload_pack(pack_path, name, encrypted, len(plaintext), 1)
    write_synthetic_pe(exe_path, resource_key)

    def fail_if_called(_args):
        raise AssertionError("QLIE script export must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    preview_code = cli.main(
        [
            "qlie",
            "export-scripts",
            str(game_dir),
            "--output",
            str(output_dir),
            "--archive",
            "data.pack",
            "--exe",
            str(exe_path),
            "--json",
        ]
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview_code == 0
    assert preview["status"] == "ready"
    assert not output_dir.exists()

    apply_code = cli.main(
        [
            "qlie",
            "export-scripts",
            str(game_dir),
            "--output",
            str(output_dir),
            "--archive",
            "data.pack",
            "--exe",
            str(exe_path),
            "--apply",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert apply_code == 0
    assert result["status"] == "exported"
    assert (
        output_dir / "resolved" / "scenario" / "main.s"
    ).read_bytes() == plaintext


def test_qlie_survey_scripts_json_is_content_free_and_does_not_build_agent(
    tmp_path,
    monkeypatch,
    capsys,
):
    export_dir = tmp_path / "export"
    script_path = export_dir / "resolved" / "main.s"
    script_path.parent.mkdir(parents=True)
    secret_text = "NeverEmitThisDialogue"
    data = b"\xff\xfe" + secret_text.encode("utf-16-le")
    script_path.write_bytes(data)
    manifest = {
        "items": [
            {
                "archive_name": "GameData/data6.pack",
                "conflict_group": None,
                "decoded_sha256": hashlib.sha256(data).hexdigest(),
                "decoded_size": len(data),
                "internal_path": "main.s",
                "output_path": "resolved/main.s",
                "status": "exported",
            }
        ]
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fail_if_called(_args):
        raise AssertionError("QLIE script survey must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    exit_code = cli.main(
        ["qlie", "survey-scripts", str(export_dir), "--top-shapes", "5", "--json"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["status"] == "supported"
    assert payload["summary"]["surveyed_count"] == 1
    assert secret_text not in captured.out


def test_qlie_parse_script_defaults_to_metadata_and_requires_explicit_text_emit(
    tmp_path,
    monkeypatch,
    capsys,
):
    export_dir = tmp_path / "export"
    script_path = export_dir / "resolved" / "main.s"
    script_path.parent.mkdir(parents=True)
    secret_text = "「NeverEmitByDefault」"
    data = (secret_text + "\r\n").encode("utf-8")
    script_path.write_bytes(data)
    manifest = {
        "items": [
            {
                "archive_name": "GameData/data6.pack",
                "conflict_group": None,
                "decoded_sha256": hashlib.sha256(data).hexdigest(),
                "decoded_size": len(data),
                "entry_index": 3,
                "internal_path": "main.s",
                "output_path": "resolved/main.s",
                "status": "exported",
            }
        ]
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fail_if_called(_args):
        raise AssertionError("QLIE script parsing must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    summary_code = cli.main(
        [
            "qlie",
            "parse-script",
            str(export_dir),
            "--path",
            "resolved/main.s",
            "--json",
        ]
    )
    summary_output = capsys.readouterr().out
    summary = json.loads(summary_output)
    emit_code = cli.main(
        [
            "qlie",
            "parse-script",
            str(export_dir),
            "--path",
            "resolved/main.s",
            "--emit-jsonl",
        ]
    )
    emitted = capsys.readouterr().out

    assert summary_code == 0
    assert summary["status"] == "supported"
    assert summary["summary"]["translatable_count"] == 1
    assert "segments" not in summary
    assert secret_text not in summary_output
    assert emit_code == 0
    assert secret_text in emitted


def test_qlie_build_corpus_defaults_to_dry_run_and_requires_apply(
    tmp_path,
    monkeypatch,
    capsys,
):
    export_dir = tmp_path / "export"
    output_dir = tmp_path / "corpus"
    script_path = export_dir / "resolved" / "main.s"
    script_path.parent.mkdir(parents=True)
    secret_text = "「CorpusTextOnlyOnApply」"
    data = (secret_text + "\r\n").encode("utf-8")
    script_path.write_bytes(data)
    manifest = {
        "schema_version": 1,
        "engine": "qlie",
        "items": [
            {
                "archive_name": "GameData/data6.pack",
                "conflict_group": None,
                "decoded_sha256": hashlib.sha256(data).hexdigest(),
                "decoded_size": len(data),
                "entry_index": 3,
                "internal_path": "main.s",
                "output_path": "resolved/main.s",
                "status": "exported",
            }
        ],
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fail_if_called(_args):
        raise AssertionError("QLIE corpus generation must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)

    dry_run_code = cli.main(
        [
            "qlie",
            "build-corpus",
            str(export_dir),
            "--output",
            str(output_dir),
            "--json",
        ]
    )
    dry_run_output = capsys.readouterr().out
    dry_run = json.loads(dry_run_output)
    apply_code = cli.main(
        [
            "qlie",
            "build-corpus",
            str(export_dir),
            "--output",
            str(output_dir),
            "--apply",
            "--json",
        ]
    )
    apply_output = capsys.readouterr().out
    result = json.loads(apply_output)

    assert dry_run_code == 0
    assert dry_run["status"] == "ready"
    assert secret_text not in dry_run_output
    assert apply_code == 0
    assert result["status"] == "published"
    assert secret_text in (output_dir / "segments.jsonl").read_text(encoding="utf-8")
    assert secret_text not in (output_dir / "parse-report.json").read_text(encoding="utf-8")
