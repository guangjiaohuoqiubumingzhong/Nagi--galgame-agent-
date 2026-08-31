import json
import os

from nagi.gameio.qlie.exporter import apply_script_export_plan, build_script_export_plan
from nagi.gameio.qlie.archive import _filename_seed31
from test_qlie_payload import (
    encrypt_normal_file,
    make_literal_bpe,
    write_payload_pack,
    write_synthetic_pe,
)
from test_qlie_toc import write_filepack31


def test_plan_requires_explicit_precedence_for_duplicate_paths(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    write_filepack31(
        game_dir / "data6.pack",
        [
            ("scenario\\root.s", 0, 1, 10, 1, 2),
            ("scenario\\unique.s", 1, 1, 8, 1, 2),
        ],
    )
    write_filepack31(
        game_dir / "data8.pack",
        [("scenario\\root.s", 0, 1, 12, 1, 2)],
    )

    plan = build_script_export_plan(
        game_dir,
        tmp_path / "export",
        conflict_policy="precedence",
    )

    assert plan.status == "needs_precedence"
    root_items = [item for item in plan.items if item.internal_path == "scenario\\root.s"]
    assert {item.status for item in root_items} == {"conflict"}
    assert next(item for item in plan.items if item.internal_path.endswith("unique.s")).status == "selected"


def test_explicit_archive_order_selects_later_duplicate(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    write_filepack31(
        game_dir / "data6.pack",
        [("scenario\\root.s", 0, 1, 10, 1, 2)],
    )
    write_filepack31(
        game_dir / "data8.pack",
        [("scenario\\root.s", 0, 1, 12, 1, 2)],
    )

    plan = build_script_export_plan(
        game_dir,
        tmp_path / "export",
        archives=["data6.pack", "data8.pack"],
        conflict_policy="precedence",
    )

    assert plan.status == "ready"
    assert plan.archive_order == ("data6.pack", "data8.pack")
    selected = next(item for item in plan.items if item.status == "selected")
    shadowed = next(item for item in plan.items if item.status == "shadowed")
    assert selected.archive_name == "data8.pack"
    assert selected.output_path == "scenario/root.s"
    assert shadowed.shadowed_by == "data8.pack#0"


def test_preserve_policy_layers_duplicates_without_claiming_a_winner(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    write_filepack31(
        game_dir / "data6.pack",
        [
            ("scenario\\root.s", 0, 1, 10, 1, 2),
            ("scenario\\unique.s", 1, 1, 8, 1, 2),
        ],
    )
    write_filepack31(
        game_dir / "data8.pack",
        [("scenario\\root.s", 0, 1, 12, 1, 2)],
    )

    plan = build_script_export_plan(game_dir, tmp_path / "export")

    assert plan.status == "ready"
    assert plan.conflict_policy == "preserve"
    root_items = [item for item in plan.items if item.internal_path == "scenario\\root.s"]
    assert {item.status for item in root_items} == {"preserved"}
    assert {item.conflict_group for item in root_items} == {"scenario/root.s"}
    assert {item.output_path for item in root_items} == {
        "layers/data6.pack/scenario/root.s",
        "layers/data8.pack/scenario/root.s",
    }
    unique = next(item for item in plan.items if item.internal_path.endswith("unique.s"))
    assert unique.status == "selected"
    assert unique.output_path == "resolved/scenario/unique.s"
    assert plan.to_dict()["summary"]["selected_count"] == 3


def test_default_scope_prefers_gamedata_over_installer_pack(tmp_path):
    game_dir = tmp_path / "game"
    game_data_dir = game_dir / "GameData"
    game_data_dir.mkdir(parents=True)
    write_filepack31(
        game_data_dir / "data0.pack",
        [("scenario\\main.s", 0, 1, 10, 1, 2)],
    )
    write_filepack31(
        game_dir / "UnInstallData.pack",
        [("uninstallfiles.txt", 0, 1, 10, 1, 1)],
    )

    plan = build_script_export_plan(game_dir, tmp_path / "export")

    assert plan.status == "ready"
    assert plan.archive_order == ("GameData/data0.pack",)
    assert [item.internal_path for item in plan.items] == ["scenario\\main.s"]


def test_plan_blocks_unsafe_internal_and_overlapping_output_paths(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    write_filepack31(
        game_dir / "data.pack",
        [("..\\escape.s", 0, 1, 1, 0, 2)],
    )

    unsafe_entry = build_script_export_plan(
        game_dir,
        tmp_path / "export",
        archives=["data.pack"],
    )
    overlapping = build_script_export_plan(
        game_dir,
        game_dir / "export",
        archives=["data.pack"],
    )

    assert unsafe_entry.status == "blocked"
    assert unsafe_entry.items[0].status == "rejected"
    assert overlapping.status == "unsafe_output"


def test_plan_filters_extensions_and_is_stable(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    write_filepack31(
        game_dir / "data.pack",
        [
            ("scenario\\main.s", 0, 1, 3, 1, 2),
            ("notes\\readme.txt", 1, 1, 4, 1, 2),
            ("image\\background.png", 2, 1, 5, 1, 2),
        ],
    )

    plan = build_script_export_plan(
        game_dir,
        tmp_path / "export",
        archives=["data.pack"],
        extensions=["s"],
    )

    assert plan.status == "ready"
    assert plan.extensions == (".s",)
    assert [item.internal_path for item in plan.items] == ["scenario\\main.s"]
    assert plan.to_json() == plan.to_json()


def test_apply_exports_to_new_directory_and_writes_manifest(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pack_path = game_dir / "data.pack"
    exe_path = game_dir / "game.exe"
    output_dir = tmp_path / "export"
    name = "scenario\\main.s"
    plaintext = "@@@AVG\\header.s\r\nsynthetic fixture\r\n".encode("utf-16-le")
    resource_key = bytes(range(256))
    seed = _filename_seed31(bytes(range(256)))
    compressed = make_literal_bpe(plaintext)
    encrypted = encrypt_normal_file(name, compressed, seed, resource_key)
    pack_content, _ = write_payload_pack(pack_path, name, encrypted, len(plaintext), 1)
    write_synthetic_pe(exe_path, resource_key)
    pack_modified_before = pack_path.stat().st_mtime_ns

    plan = build_script_export_plan(
        game_dir,
        output_dir,
        archives=["data.pack"],
    )
    result = apply_script_export_plan(plan, exe_path=exe_path)

    exported_path = output_dir / "resolved" / "scenario" / "main.s"
    manifest_path = output_dir / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert result.status == "exported"
    assert exported_path.read_bytes() == plaintext
    assert manifest["summary"] == {
        "decoded_bytes": len(plaintext),
        "exported_count": 1,
    }
    assert manifest["items"][0]["status"] == "exported"
    assert manifest["items"][0]["decoded_sha256"]
    assert "synthetic fixture" not in manifest_text
    assert pack_path.read_bytes() == pack_content
    assert pack_path.stat().st_mtime_ns == pack_modified_before

    repeated = apply_script_export_plan(plan, exe_path=exe_path)
    assert repeated.status == "output_exists"


def test_apply_retries_transient_directory_commit_lock(tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pack_path = game_dir / "data.pack"
    exe_path = game_dir / "game.exe"
    output_dir = tmp_path / "export"
    name = "scenario\\main.s"
    plaintext = b"fixture"
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
    plan = build_script_export_plan(
        game_dir,
        output_dir,
        archives=["data.pack"],
    )
    real_rename = os.rename
    rename_calls = 0

    def transiently_locked_rename(source, target):
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            raise PermissionError(5, "synthetic transient lock")
        real_rename(source, target)

    monkeypatch.setattr("nagi.gameio.qlie.exporter.os.rename", transiently_locked_rename)
    monkeypatch.setattr("nagi.gameio.qlie.exporter.time.sleep", lambda _delay: None)

    result = apply_script_export_plan(plan, exe_path=exe_path)

    assert result.status == "exported"
    assert rename_calls == 2
    assert (output_dir / "resolved" / "scenario" / "main.s").read_bytes() == plaintext


def test_apply_preserves_both_conflicting_archive_variants(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    exe_path = game_dir / "game.exe"
    output_dir = tmp_path / "export"
    name = "scenario\\main.s"
    resource_key = bytes(range(256))
    seed = _filename_seed31(bytes(range(256)))
    variants = {
        "data6.pack": b"synthetic data6 variant",
        "data8.pack": b"synthetic data8 variant",
    }
    for archive_name, plaintext in variants.items():
        encrypted = encrypt_normal_file(
            name,
            make_literal_bpe(plaintext),
            seed,
            resource_key,
        )
        write_payload_pack(
            game_dir / archive_name,
            name,
            encrypted,
            len(plaintext),
            1,
        )
    write_synthetic_pe(exe_path, resource_key)

    plan = build_script_export_plan(game_dir, output_dir)
    result = apply_script_export_plan(plan, exe_path=exe_path)

    assert result.status == "exported"
    for archive_name, plaintext in variants.items():
        exported = output_dir / "layers" / archive_name / "scenario" / "main.s"
        assert exported.read_bytes() == plaintext
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["conflict_policy"] == "preserve"
    assert {item["conflict_group"] for item in manifest["items"]} == {
        "scenario/main.s"
    }


def test_failed_decode_does_not_create_output_directory(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pack_path = game_dir / "data.pack"
    exe_path = game_dir / "wrong.exe"
    output_dir = tmp_path / "export"
    name = "scenario\\main.s"
    plaintext = b"fixture"
    correct_key = bytes(range(256))
    wrong_key = bytes(reversed(range(256)))
    seed = _filename_seed31(bytes(range(256)))
    encrypted = encrypt_normal_file(name, make_literal_bpe(plaintext), seed, correct_key)
    write_payload_pack(pack_path, name, encrypted, len(plaintext), 1)
    write_synthetic_pe(exe_path, wrong_key)
    plan = build_script_export_plan(
        game_dir,
        output_dir,
        archives=["data.pack"],
    )

    result = apply_script_export_plan(plan, exe_path=exe_path)

    assert result.status == "decode_failed"
    assert not output_dir.exists()
