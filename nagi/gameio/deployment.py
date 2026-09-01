"""Dispatch verified engine deployments; retain legacy YU-RIS pilot support."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from ..locale_emulator import LocaleEmulatorSettings
from ..game_launcher import write_runtime_launcher
from ..paths import application_root, state_root
from ..translation_storage import deployment_paths, publish_playable
from .deployment_runtime.locale_support import japanese_profile

PROFILE_ID = "yuris479-rikka-approved-opening-v1"
GAME_FILES = {
    "M.C.2催眠研究.exe": "50ec52960b55ce3eb4a5291c5c4c37096c717af2469fbc27e7f27cd5438d9aea",
    "pac/ysbin.ypf": "d4b3b2caf38e610ec79f10b6b3a440e9636de841997f166eb8360af8eda2038c",
    "pac/bgm.ypf": "047f25f7c471d54f194edc9a5942701bb8838ffc3424d98a472650284de8093f",
    "pac/cg.ypf": "edf3190f59c9fb52e3ea752aef5e04dfce686a30806b5a8358ae81c9ee47a1bb",
    "pac/cgsys.ypf": "ee5d3ffa4ad25a2545944e3fb2c4b97bdedaa05fd001f897d1d036d0ec32e975",
    "pac/se.ypf": "fc3422ab494d46a2808df05bcc3ad6e7f2c8ae24996d41fd55112320a28db918",
    "pac/voice.ypf": "33cf2c98a82754d6416ed025205cd4cce42a5f829506ff3e5fbdcceb0c4cc86d",
}
TRIAL_FILES = {
    "packed-source.ypf": "e123dc143e689bd83da66e47b53d62028a691a197f08f0d3717fb46edd5b504c",
    "display-map.json": "902805faf3fbdaa7fac0562d9b0b91785bf5c167c804f289a9113fe032e525a1",
    "translations.json": "07cf04beb4ea05ccac9d35ddef01c627f8da132a350d6c971b8303bbb898af39",
    "validation.json": "bebcc2d06e705bf892ee2a15234339d293d28fd5d1d3ab01fb738fd82222f367",
    "mapping.json": "75dc399b9636b8f66e37e02cb684ad01a0953ea9f69ac401d7c025a77fe1a318",
    "review-edits.json": "89fb0b6f3d443b3ca380c03373952dbbfaa658def4f369d7016b61b0b220398e",
    "approved-corpus/segments.jsonl": "d9a437f0696592e585e769bd65f8432d96ff42c9454bc4dbf7c1afd4551d1830",
    "approved-corpus/parse-report.json": "71437029cc940fb79c6d0e4352c6da31fad84d8840c179f09b17c39c1d9df85a",
}
LOCALE_FILES = {
    "LECommonLibrary.dll": "6edbb48b891e3ee830e7b487f352d90044474a757905ff49d3b2abfdb980c211",
    "LEConfig.xml": "95b50272d855e9bf47fea5176da85b19cae394e50ed1a6e9101d98f9d3758f75",
    "LEProc.exe": "2a81cb20b5b705d84416fd8b1cd9a819b1cafaeccc0c1e23c289884310ec0461",
    "LEVersion.xml": "2dc3fd0b9c2b16ac4984c60fcfa478fe391a93333013ccefecae39d0b77f0ba4",
    "LoaderDll.dll": "82fae0f44f4ca0c9c37907df74cef2415eeb5fae1cf8d4f36f34ffcaf7e3cc0c",
    "LocaleEmulator.dll": "c79c175fdad174aa46a72197d148316299a56f950aaab1b84930d09ee1084a88",
}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def safe_file(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
        raise ValueError(f"文件缺失或路径不安全：{relative}")
    if any(
        p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
        for p in (path, *path.parents)
        if p != root and root in p.parents
    ):
        raise ValueError(f"不允许链接文件：{relative}")
    return path


def check_files(root, expected):
    for name, checksum in expected.items():
        if digest(safe_file(root, name)) != checksum:
            raise ValueError(f"文件与已验证版本不一致，停止部署：{name}")


def copy_checked(source, target, checksum, on_bytes=lambda _count: None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    checksum_actual = hashlib.sha256()
    with Path(source).open("rb") as src, target.open("xb") as dst:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            dst.write(block)
            checksum_actual.update(block)
            on_bytes(len(block))
    if checksum_actual.hexdigest() != checksum or digest(target) != checksum:
        raise ValueError(f"复制校验失败：{target.name}")


def trial_root():
    project = application_root()
    archived = (
        state_root()
        / "legacy-workspace/YU-RIS/yuris-experiments/mc2-rikka-20260830/pilot-32"
    )
    return (
        archived
        if archived.is_dir()
        else project.parent / "yuris-experiments/mc2-rikka-20260830/pilot-32"
    )


def restore_trial(item, job_factory):
    """Reuse actual completed artifacts, not fake completion or new model calls."""
    root, source = Path(item.output_root), trial_root()
    check_files(
        item.game_dir,
        {name: GAME_FILES[name] for name in ("M.C.2催眠研究.exe", "pac/ysbin.ypf")},
    )
    check_files(source, TRIAL_FILES)
    locale = source / "game/locale-fix/LE"
    check_files(locale, LOCALE_FILES)
    root.mkdir(parents=True, exist_ok=False)
    snapshot = root / "verified-translation"
    for ordinal, (relative, checksum) in enumerate(TRIAL_FILES.items(), 1):
        if item.job.cancel_event.is_set():
            raise InterruptedError()
        copy_checked(safe_file(source, relative), snapshot / relative, checksum)
        item.job.update(
            progress=0.34 * ordinal / len(TRIAL_FILES),
            message="正在核对并复用已完成的试译资料",
        )
    for relative, checksum in LOCALE_FILES.items():
        copy_checked(
            safe_file(locale, relative), snapshot / "locale" / relative, checksum
        )
    corpus = str(snapshot / "approved-corpus")
    with item.lock:
        item.extraction_job.update(
            export_dir=corpus, corpus_dir=corpus, status="completed"
        )
        item.stages["extract"].update(
            status="completed",
            progress=1,
            error=None,
            message="已复用核验通过的开场提取资料",
        )
        translated = job_factory(
            uuid4().hex[:12], item.game_dir, "pilot", str(snapshot)
        )
        translated.update(
            status="completed",
            progress=1,
            total_units=29,
            translated_units=29,
            translated_scripts_dir=str(snapshot),
            message="已复用 29 条校验通过的译文；3 条原文保持不变，未调用 API",
        )
        item.translation_job = translated
        item.job = translated
        item.source_kind = PROFILE_ID
        item.stages["translate"].update(
            status="completed", progress=1, error=None, message=translated.message
        )
        item.stages["deploy"].update(message="试译结果已就绪，可以生成汉化启动文件")


def deploy_workflow(item, progress):
    if getattr(item, "source_kind", None) in {"kirikiri", "renpy", "tyranoscript"}:
        from .text_engines import deploy_game

        return deploy_game(item, progress)
    if getattr(item, "source_kind", None) == "yuris-479":
        return deploy_full_yuris(item, progress)
    if getattr(item, "source_kind", None) == "qlie" or getattr(item, "engine_family", None) == "QLIE":
        from .qlie.deployment import deploy_qlie

        return deploy_qlie(item, progress)
    if (
        getattr(item, "source_kind", None) != PROFILE_ID
        or item.translation_job.mode != "pilot"
    ):
        raise ValueError(
            "当前任务不属于已适配的五种引擎部署类型；旧开场试译请通过“复用已完成试译”进入。"
        )
    if os.name != "nt" or importlib.util.find_spec("frida") is None:
        raise ValueError(
            "部署需要 Windows 和本地 Frida 字体适配依赖；不会自动下载安装。"
        )
    root = Path(item.output_root).resolve()
    game = Path(item.game_dir).resolve()
    snapshot = Path(item.translation_job.translated_scripts_dir).resolve()
    if root not in snapshot.parents or game == root or game in root.parents:
        raise ValueError("部署输出不能覆盖原游戏，且必须使用当前任务的已验证译文。")
    progress(0.02, "校验游戏版本、译文和中文字体映射")
    check_files(game, GAME_FILES)
    check_files(snapshot, TRIAL_FILES)
    check_files(snapshot / "locale", LOCALE_FILES)
    total = sum(safe_file(game, p).stat().st_size for p in GAME_FILES) + 4 * 1024 * 1024
    if shutil.disk_usage(root).free < total + 64 * 1024 * 1024:
        raise ValueError("保存目录的剩余空间不足以创建独立游戏副本。")
    staging, output = deployment_paths(item, game)
    staging.mkdir(exist_ok=False)
    copied = 0
    manifest_files = {}

    def advance(size):
        nonlocal copied
        copied += size
        progress(
            0.12 + 0.68 * min(1, copied / total),
            "正在复制并校验独立游戏资源；不复制原存档",
        )

    try:
        for relative, checksum in GAME_FILES.items():
            if relative == "pac/ysbin.ypf":
                source, checksum = (
                    snapshot / "packed-source.ypf",
                    TRIAL_FILES["packed-source.ypf"],
                )
            else:
                source = safe_file(game, relative)
            target = "game/" + relative
            copy_checked(source, staging / target, checksum, advance)
            manifest_files[target] = checksum
        progress(0.82, "安装日区启动组件和中文字体适配")
        for relative, checksum in LOCALE_FILES.items():
            target = "game/locale-fix/LE/" + relative
            copy_checked(
                safe_file(snapshot / "locale", relative), staging / target, checksum
            )
            manifest_files[target] = checksum
        for relative in ("display-map.json",):
            copy_checked(snapshot / relative, staging / relative, TRIAL_FILES[relative])
            manifest_files[relative] = TRIAL_FILES[relative]
        assets = Path(__file__).parent / "deployment_runtime"
        for relative in ("launch.py", "display_bridge.js", "locale_support.py"):
            target = "runtime/" + relative
            checksum = digest(assets / relative)
            copy_checked(assets / relative, staging / target, checksum)
            manifest_files[target] = checksum
        manifest = {
            "schema_version": 1,
            "profile": PROFILE_ID,
            "translated_count": 29,
            "selected_count": 32,
            "unchanged_ordinals": [10, 19, 22],
            "executable": "game/M.C.2催眠研究.exe",
            "files": manifest_files,
            "original_game": str(game),
            "requires_font_bridge": True,
        }
        (staging / "deployment.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # No powershell execution-policy changes. cmd expands its own directory
        # once; delayed expansion is off and the interpreter path is escaped.
        launcher_name = "启动汉化版.cmd"
        write_runtime_launcher(staging)
        (staging / "使用说明.txt").write_text(
            "双击“启动汉化版.cmd”，选择从头开始查看开场试译。\n"
            "前32条中29条已翻译，第10、19、22条及其余内容保持日文。\n"
            "本入口依赖 Nagi 运行时；移动程序后双击“重新定位Nagi.cmd”选择新目录。\n"
            "只操作本文件夹的游戏副本；原游戏和原存档不变。不要直接运行game目录里的exe。\n"
            "启动时自动加载字体适配，启动失败请查看launch-error.log。关闭游戏后辅助进程自动退出。\n",
            encoding="utf-8",
        )
        progress(0.94, "校验启动文件和部署清单")
        check_files(staging, manifest_files)
        # Publish only a complete directory. Failed partial copies are kept for
        # inspection, never exposed as a successful launcher, never overwritten.
        publish_playable(staging, output)
        progress(0.99, "独立汉化启动文件已生成")
        return output / launcher_name
    except Exception:  # Record and preserve partial outputs, then re-raise.
        (staging / "FAILED.txt").write_text(
            "部署未完成，不要运行此目录；可人工检查或删除。", encoding="utf-8"
        )
        raise


def yuris_assets(snapshot, game):
    """Build runtime assets only on deployment; accept earlier binary receipts."""
    from .yuris import archive_entries, catalog, patched_archive, sha

    snapshot = Path(snapshot)
    result = json.loads(
        safe_file(snapshot, "translation-result.json").read_text(encoding="utf-8")
    )
    if (
        result.get("engine") != "yuris-479"
        or result.get("status") != "completed"
        or not result.get("text_count")
        or result.get("translated_count") != result["text_count"]
    ):
        raise ValueError("本次所选范围的 API 结果尚不完整，不能用旧试译替代")
    scope = result.get("mode", "full")
    if scope not in {"full", "partial"}:
        raise ValueError("无效的翻译范围记录")
    if result.get("storage_format") == "text-only-v1":
        source = safe_file(game, "pac/ysbin.ypf").read_bytes()
        translations = safe_file(snapshot, "translations.json").read_bytes()
        if (
            sha(source) != result["archive_sha256"]
            or sha(translations) != result["translations_sha256"]
        ):
            raise ValueError("原游戏或 API 译文已改变，停止部署")
        combined = json.loads(translations)
        if len(combined) != result["translated_count"]:
            raise ValueError("API 译文数量与完成记录不一致")
        selected_ids = None
        if scope == "partial":
            from .yuris_opening import selected_for_result

            entries = archive_entries(source)
            table, key, units = catalog(entries)
            selected = selected_for_result(entries, table, key, units, result)
            if (
                result.get("selection_limit") != 50
                or result.get("source_text_count") != len(units)
                or result["text_count"] != len(selected)
                or result["units_sha256"] != sha(
                    json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()
                )
            ):
                raise ValueError("部分翻译结果与已记录的选取范围不匹配")
            selected_ids = [unit["id"] for unit in selected]
        glossary = None
        if result.get("character_glossary_version"):
            from ..translation.characters import GLOSSARY_FILE, load_glossary

            glossary = load_glossary(safe_file(snapshot, GLOSSARY_FILE),
                                     expected_version=result["character_glossary_version"])
        packed, glyphs = patched_archive(source, combined, selected_ids=selected_ids,
                                         character_glossary=glossary)
        mapping = json.dumps(
            {"schema_version": 1, "encoding": "cp932-unicode-bridge", "glyphs": glyphs},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        result = {
            **result,
            "packed_sha256": sha(packed),
            "display_map_sha256": sha(mapping),
        }
    else:
        if scope != "full":
            raise ValueError("部分翻译需要当前文本结果格式")
        check_files(
            snapshot,
            {
                "translated.ypf": result["packed_sha256"],
                "display-map.json": result["display_map_sha256"],
            },
        )
        packed = (snapshot / "translated.ypf").read_bytes()
        mapping = (snapshot / "display-map.json").read_bytes()
    return result, packed, mapping


def deploy_full_yuris(item, progress):
    """Publish this workflow's selected API result, never the legacy pilot."""
    if os.name != "nt" or importlib.util.find_spec("frida") is None:
        raise ValueError("YU-RIS 中文启动需要 Windows 和已安装的 Frida 字体适配依赖")
    root, game = Path(item.output_root).resolve(), Path(item.game_dir).resolve()
    snapshot = Path(item.translation_job.translated_scripts_dir).resolve()
    if root not in snapshot.parents or game == root or game in root.parents:
        raise ValueError("汉化输出必须位于本次独立任务目录，不得覆盖原游戏")
    if item.translation_job.mode not in {"full", "partial"}:
        raise ValueError("YU-RIS 部署必须使用本次所选范围的翻译结果")
    settings = LocaleEmulatorSettings(getattr(item, "locale_settings_path", None))
    settings.snapshot()  # Read-only component check; never execute on selection.
    progress(0.02, "确认当前翻译资源包、游戏版本与中文字体映射")
    check_files(game, GAME_FILES)
    result, packed, mapping = yuris_assets(snapshot, game)
    if result.get("mode", "full") != item.translation_job.mode:
        raise ValueError("翻译结果与本次选择的范围不匹配")
    if result["archive_sha256"] != GAME_FILES["pac/ysbin.ypf"]:
        raise ValueError("译文的原始脚本包与所选游戏不匹配")
    staging, output = deployment_paths(item, game)
    total = sum(safe_file(game, name).stat().st_size for name in GAME_FILES)
    if shutil.disk_usage(staging.parent).free < total + len(packed) + 64 * 1024 * 1024:
        raise ValueError("空间不足，无法创建完整的独立汉化游戏副本")
    staging.mkdir(exist_ok=False)
    files, copied = {}, 0

    def advance(size):
        nonlocal copied
        copied += size
        progress(
            0.1 + 0.7 * min(1, copied / total), "正在复制独立游戏并嵌入本次汉化包"
        )

    try:
        for relative, checksum in GAME_FILES.items():
            target = "game/" + relative
            if relative == "pac/ysbin.ypf":
                checksum = result["packed_sha256"]
                (staging / target).parent.mkdir(parents=True, exist_ok=True)
                (staging / target).write_bytes(packed)
                advance(len(packed))
            else:
                copy_checked(
                    safe_file(game, relative), staging / target, checksum, advance
                )
            files[target] = checksum
        profile = "game/M.C.2催眠研究.exe.le.config"
        (staging / profile).write_bytes(japanese_profile())
        files[profile] = digest(staging / profile)
        progress(0.84, "安装 Unicode 中文显示和独立启动入口")
        for relative in ("launch.py", "display_bridge.js", "locale_support.py"):
            source = Path(__file__).parent / "deployment_runtime" / relative
            target = "runtime/" + relative
            files[target] = digest(source)
            copy_checked(source, staging / target, files[target])
        files["display-map.json"] = result["display_map_sha256"]
        (staging / "display-map.json").write_bytes(mapping)
        manifest = {
            "schema_version": 1,
            "profile": f"yuris479-rikka-{item.translation_job.mode}-v1",
            "translated_count": result["translated_count"],
            "selected_count": result["text_count"],
            "unchanged_ordinals": [],
            "unselected_count": result.get("source_text_count", result["text_count"]) - result["text_count"],
            "executable": "game/M.C.2催眠研究.exe",
            "files": files,
            "original_game": str(game),
            "requires_font_bridge": True,
            "translation_result_sha256": digest(snapshot / "translation-result.json"),
            "locale_settings_path": str(settings.path.resolve()),
        }
        (staging / "deployment.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        launcher_name = "启动汉化版.cmd"
        write_runtime_launcher(staging)
        (staging / "使用说明.txt").write_text(
            f"双击“启动汉化版.cmd”启动独立汉化游戏。\n本次 API 已处理 {result['translated_count']} 条文本。\n"
            "启动时自动启用日区和中文字体；不要直接运行 game 内的 EXE。\n"
            "原游戏、原存档保持不变。该入口依赖 Nagi 运行时；移动程序后双击“重新定位Nagi.cmd”。\n"
            "Locale Emulator 使用 Nagi 中保存的本地目录；移动或更新组件后请重新选择并保存。\n"
            "窗口正常关闭后字体辅助进程自动退出；故障日志为 launch-error.log。\n",
            encoding="utf-8",
        )
        progress(0.95, "确认副本及启动组件完整")
        check_files(staging, files)
        publish_playable(staging, output)
        return output / "启动汉化版.cmd"
    except Exception:
        (staging / "FAILED.txt").write_text(
            "部署未完成，此目录不能作为汉化入口。", encoding="utf-8"
        )
        raise
