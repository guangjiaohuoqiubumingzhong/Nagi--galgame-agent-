"""Verified FilePack 3.1 deployment to an independent, Unicode-capable copy."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path

from ...game_launcher import write_runtime_launcher
from ...locale_emulator import LocaleEmulatorSettings
from ...translation.patch import (
    _apply_byte_diffs,
    _decode_preview_slice_encoding,
    _validate_byte_diffs,
    _verify_current_corpus,
    _verify_published_preview,
)
from ...translation_storage import (
    deployment_paths,
    publish_playable,
    require_separate_output,
)
from ..deployment import check_files, copy_checked, digest, safe_file
from ..deployment_runtime.locale_support import japanese_profile
from .archive import inspect_filepack_toc
from .exporter import _resolve_archives, _safe_output_path
from .payload import read_filepack_entry
from .pe import load_reskey_from_pe
from .repack import repack_scripts
from .script import _decode_script

# The renderer and UTF-16 script loader are tested against this executable.
# Filenames are not identities; renamed copies of this binary also work.
SUPPORTED_EXECUTABLES = {
    "ad02b1115887b8e5b453d4b5760077e8b63e153bba1161e1704209cf3db5cda5": "qlie31-biman4-unicode-v1",
}


def executable_profile(game):
    matches = []
    for path in Path(game).glob("*.exe"):
        if path.is_symlink():
            continue
        checksum = digest(path)
        if checksum in SUPPORTED_EXECUTABLES:
            matches.append((path, checksum, SUPPORTED_EXECUTABLES[checksum]))
    if len(matches) != 1:
        raise ValueError(
            "QLIE 嵌入目前仅适配《美少女万华镜 -罪与罚的少女-》的已验证原版引擎；未找到唯一匹配的程序版本"
        )
    return matches[0]


def translation_assets(item, executable):
    """Reconcile the exact preview, applied text, extraction and original pack."""
    root, game = Path(item.output_root).resolve(), Path(item.game_dir).resolve()
    job = item.translation_job
    snapshot = Path(job.translated_scripts_dir).resolve()
    export = Path(item.extraction_job.export_dir).resolve()
    job_root = Path(job.output_root).resolve()
    if (
        root not in snapshot.parents
        or root not in export.parents
        or root not in job_root.parents
        or job_root not in snapshot.parents
    ):
        raise ValueError("QLIE 部署必须使用本次任务的独立译文目录")
    require_separate_output(root, game)
    if (
        job.status != "completed"
        or not job.total_units
        or job.translated_units != job.total_units
    ):
        raise ValueError("本次 QLIE API 翻译尚未完整完成，不能部署")
    _, preview, spec, diffs, preview_raw = _verify_published_preview(
        job_root / "translation-preview"
    )
    corpus = Path(spec["corpus_dir"]).resolve()
    if (
        corpus != Path(item.extraction_job.corpus_dir).resolve()
        or root not in corpus.parents
    ):
        raise ValueError("QLIE 译文引用了其他任务的语料")
    _verify_current_corpus(spec)
    applied = json.loads(
        safe_file(snapshot, "translation-apply.json").read_text(encoding="utf-8")
    )
    import hashlib

    if (
        applied.get("dry_run") is not False
        or applied.get("preview_id") != preview["preview_id"]
        or applied.get("preview_sha256") != hashlib.sha256(preview_raw).hexdigest()
        or Path(applied["source_root"]).resolve() != export
        or applied.get("changed_unit_count") != len(diffs)
        or not diffs
    ):
        raise ValueError("QLIE 译文发布记录与本次预览不一致")
    manifest = json.loads(
        safe_file(export, "manifest.json").read_text(encoding="utf-8")
    )
    if Path(manifest["game_dir"]).resolve() != game:
        raise ValueError("QLIE 提取来源不是当前游戏")
    entries = {}
    for entry in manifest["items"]:
        if entry.get("status") != "exported":
            continue
        relative = _safe_output_path(entry["output_path"])
        if relative in entries:
            raise ValueError("QLIE 提取清单包含重复路径")
        entries[relative] = entry
    grouped = defaultdict(list)
    for diff in diffs:
        grouped[_safe_output_path(diff["output_path"])].append(diff)
    resource_key = load_reskey_from_pe(executable)
    replacements = defaultdict(dict)
    for relative, file_diffs in grouped.items():
        entry = entries.get(relative)
        if entry is None:
            raise ValueError("QLIE 译文缺少原始资源定位信息")
        archive_name = _safe_output_path(entry["archive_name"])
        archive = safe_file(game, archive_name)
        original = safe_file(export, relative).read_bytes()
        if hashlib.sha256(original).hexdigest() != entry["decoded_sha256"]:
            raise ValueError("QLIE 提取脚本已改变")
        for diff in file_diffs:
            if (
                diff["source_sha256"] != entry["decoded_sha256"]
                or diff["archive_name"] != entry["archive_name"]
                or diff["internal_path"] != entry["internal_path"]
                or diff["encoding"] != file_diffs[0]["encoding"]
                or hashlib.sha256(diff["translated_text"].encode()).hexdigest()
                != diff["translated_text_sha256"]
            ):
                raise ValueError("QLIE 译文和源脚本定位不一致")
        source = read_filepack_entry(
            archive, entry_index=entry["entry_index"], resource_key=resource_key
        )
        if (
            source.data != original
            or source.report.entry.internal_path != entry["internal_path"]
            or source.report.stored_sha256 != entry["stored_sha256"]
        ):
            raise ValueError("QLIE 原资源包与提取记录不一致，请重新提取")
        encoding = file_diffs[0]["encoding"]
        ordered = _validate_byte_diffs(
            original, file_diffs, slice_codec=_decode_preview_slice_encoding(encoding)
        )
        expected = _apply_byte_diffs(
            original, ordered, encoding=encoding, fallback_encoding="utf-16-le-bom"
        )
        if safe_file(snapshot, relative).read_bytes() != expected:
            raise ValueError("QLIE 已发布译文被改动或与预览不一致")
        # The verified engine understands a UTF-16 BOM. Preserve all directives
        # and line endings while eliminating lossy CP932 Chinese conversions.
        text = _decode_script(expected)[0]
        replacements[archive_name][entry["entry_index"]] = b"\xff\xfe" + text.encode(
            "utf-16-le"
        )
    verify_effective_layers(game, replacements)
    return (
        dict(replacements),
        resource_key,
        digest(job_root / "translation-preview/preview.json"),
    )


def verify_effective_layers(game, replacements):
    """Never report success for text hidden behind a later original patch."""
    effective, selected = {}, {}
    archives, _ = _resolve_archives(Path(game).resolve(), None)
    for archive in archives:
        name = archive.relative_to(game).as_posix()
        table = inspect_filepack_toc(archive)
        if table.status != "supported" or table.archive.format_version != "3.1":
            raise ValueError("QLIE 部署不支持混合或无效的资源包版本")
        for entry in table.entries:
            path = entry.internal_path.replace("\\", "/").casefold()
            identity = (name, entry.index)
            effective[path] = identity
            if entry.index in replacements.get(name, {}):
                selected[identity] = path
    for path in selected.values():
        if effective[path] not in selected:
            raise ValueError(
                "QLIE 译文脚本被更高优先级补丁包覆盖，请重新提取并翻译当前有效脚本"
            )


def game_inventory(game, executable):
    """Only runtime assets; never bring original saves or unrelated launchers."""
    game = Path(game)
    paths = [executable]
    paths.extend(game.glob("*.dll"))
    paths.extend(game.glob("Engine*.u.txt"))
    if (game / "version.txt").is_file():
        paths.append(game / "version.txt")
    for directory in ("GameData", "DLL"):
        folder = game / directory
        if folder.is_symlink() or (
            hasattr(folder, "is_junction") and folder.is_junction()
        ):
            raise ValueError("QLIE 游戏资源目录不能是链接")
        if folder.is_dir():
            paths.extend(p for p in folder.rglob("*") if p.is_file())
    inventory = {}
    for path in paths:
        relative = path.relative_to(game).as_posix()
        inventory[relative] = digest(safe_file(game, relative))
    return inventory


def deploy_qlie(item, progress):
    if os.name != "nt" or importlib.util.find_spec("frida") is None:
        raise ValueError("QLIE 中文启动需要 Windows 和已安装的 Frida 字体适配依赖")
    game = Path(item.game_dir).resolve()
    settings = LocaleEmulatorSettings(getattr(item, "locale_settings_path", None))
    settings.snapshot()
    progress(0.02, "校验 QLIE 程序版本、本次译文与原始资源包")
    executable, _, profile = executable_profile(game)
    replacements, resource_key, preview_hash = translation_assets(item, executable)
    progress(0.07, "核对游戏运行资源，保留原游戏和原存档")
    inventory = game_inventory(game, executable)
    if not set(replacements).issubset(inventory):
        raise ValueError("QLIE 译文引用了运行目录之外的资源包")
    staging, output = deployment_paths(item, game)
    total = sum(safe_file(game, name).stat().st_size for name in inventory)
    extra = sum(len(data) for items in replacements.values() for data in items.values())
    if shutil.disk_usage(staging.parent).free < total + extra + 64 * 1024 * 1024:
        raise ValueError("空间不足，无法生成独立 QLIE 游戏副本")
    staging.mkdir(exist_ok=False)
    files, copied = {}, 0

    def advance(size):
        nonlocal copied
        copied += size
        progress(
            0.1 + 0.65 * min(1, copied / max(1, total)),
            "正在复制 QLIE 游戏并写入本次译文",
        )

    try:
        for name, checksum in inventory.items():
            source, target = safe_file(game, name), staging / "game" / name
            if name in replacements:
                result = repack_scripts(
                    source, target, replacements[name], resource_key=resource_key
                )
                if digest(source) != checksum:
                    raise ValueError("QLIE 原包在部署过程中改变")
                files["game/" + name] = result["sha256"]
                advance(source.stat().st_size)
            else:
                copy_checked(source, target, checksum, advance)
                files["game/" + name] = checksum
        progress(0.80, "安装 QLIE Unicode 中文字体和独立启动入口")
        exe_relative = "game/" + executable.name
        config = exe_relative + ".le.config"
        (staging / config).write_bytes(japanese_profile())
        files[config] = digest(staging / config)
        runtime = Path(__file__).parents[1] / "deployment_runtime"
        for name in ("launch.py", "qlie_font_bridge.js", "locale_support.py"):
            target = "runtime/" + name
            files[target] = digest(runtime / name)
            copy_checked(runtime / name, staging / target, files[target])
        (staging / "display-map.json").write_text(
            '{"schema_version":1,"glyphs":{}}', encoding="utf-8"
        )
        files["display-map.json"] = digest(staging / "display-map.json")
        manifest = {
            "schema_version": 1,
            "engine": "qlie",
            "profile": profile,
            "executable": exe_relative,
            "files": files,
            "original_game": str(game),
            "translated_count": item.translation_job.translated_units,
            "selected_count": item.translation_job.total_units,
            "changed_script_count": sum(map(len, replacements.values())),
            "mode": item.translation_job.mode,
            "requires_font_bridge": True,
            "display_bridge": "qlie_font_bridge.js",
            "preview_sha256": preview_hash,
            "locale_settings_path": str(settings.path.resolve()),
        }
        (staging / "deployment.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_runtime_launcher(staging)
        (staging / "使用说明.txt").write_text(
            "双击“启动汉化版.cmd”。本副本使用本次 QLIE 译文，原游戏和原存档未改动。\n"
            "请勿直接运行 game 内的 EXE；中文显示需要 Nagi 的 Frida 运行时和已配置的 Locale Emulator。\n"
            "仅适配已验证的 FilePack 3.1 引擎版本；图片文字不会自动翻译。\n"
            "移动 Nagi 后请使用“重新定位Nagi.cmd”，启动故障见 launch-error.log。\n",
            encoding="utf-8",
        )
        progress(0.93, "重新校验 QLIE 游戏副本与启动组件")
        check_files(staging, files)
        publish_playable(staging, output)
        return output / "启动汉化版.cmd"
    except Exception:
        (staging / "FAILED.txt").write_text(
            "QLIE 部署未完成，不能作为汉化入口。", encoding="utf-8"
        )
        raise
