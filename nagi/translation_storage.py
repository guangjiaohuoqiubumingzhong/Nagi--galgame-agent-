"""Engine-family storage layout. Versions belong in metadata, not bucket names."""

import hashlib
import re
from pathlib import Path
from uuid import uuid4


def engine_family(kind):
    value = str(kind).lower()
    if value.startswith(("yuris", "yu-ris")):
        return "YU-RIS"
    if value.startswith("qlie"):
        return "QLIE"
    raise ValueError("未识别的游戏引擎，不能选择保存目录")


def game_folder(game):
    game = Path(game).resolve()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", game.name).strip(" .")[:60] or "game"
    # Same-named installations must not share checkpoints by accident.
    suffix = hashlib.sha256(str(game).casefold().encode("utf-8")).hexdigest()[:8]
    return f"{name}-{suffix}"


def workflow_root(storage, game, family, identifier):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", identifier):
        raise ValueError("Invalid workflow identifier")
    return (
        Path(storage)
        / engine_family(family)
        / game_folder(game)
        / ("translation-" + identifier)
    )


def playable_slot(mode):
    if mode == "full":
        return "translation-formal"
    if mode in {"partial", "pilot"}:
        return "translation-test"
    raise ValueError("Invalid playable translation mode")


def playable_root(storage, output, *, mode=None):
    storage, output = Path(storage).resolve(), Path(output).resolve()
    relative = output.relative_to(storage)
    if len(relative.parts) != 3 or relative.parts[0] not in {"QLIE", "YU-RIS"}:
        raise ValueError("Invalid engine-organized workflow directory")
    target = storage.parent / "playable" / relative
    if mode is not None:
        target = target.parent / playable_slot(mode)
    return target.resolve()


def deployment_paths(item, game):
    """A temporary sibling becomes the final slot only after verification."""
    destination = require_separate_output(item.playable_root, game)
    identifier = uuid4().hex[:8]
    if getattr(item, "playable_layout_version", 1) == 2:
        if destination.name != playable_slot(item.translation_job.mode):
            raise ValueError("Playable directory does not match translation mode")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination.parent / f".deploy-{identifier}.partial", destination
    destination.mkdir(parents=True, exist_ok=True)
    return destination / f".deploy-{identifier}.partial", destination / f"localized-game-{identifier}"


def publish_playable(staging, output):
    """Archive an existing fixed slot instead of destroying a game or its saves."""
    import json

    staging, output = Path(staging), Path(output)
    if staging.is_symlink() or output.is_symlink():
        raise ValueError("Playable publish paths must not be links")
    staging, output = staging.resolve(), output.resolve()
    if staging.parent != output.parent or not staging.name.startswith(".deploy-") or not staging.name.endswith(".partial"):
        raise ValueError("Invalid playable staging directory")
    if output.name not in {"translation-test", "translation-formal"}:
        if not output.name.startswith("localized-game-") or output.exists():
            raise ValueError("Invalid legacy playable destination")
        staging.rename(output)
        return None
    backup = None
    if output.exists():
        manifest = output / "deployment.json"
        if not output.is_dir() or not manifest.is_file() or json.loads(manifest.read_text(encoding="utf-8")).get("schema_version") != 1:
            raise ValueError("目标目录不是已生成的 Nagi 游戏副本，不会移动或覆盖")
        game_root = output.parent
        playable = game_root.parent.parent
        backup = (playable / ".history" / game_root.parent.name / game_root.name
                  / f"{output.name}-{uuid4().hex[:12]}").resolve()
        if playable not in backup.parents or output in backup.parents:
            raise ValueError("Playable backup escapes its storage root")
        backup.parent.mkdir(parents=True, exist_ok=True)
        output.rename(backup)
    try:
        staging.rename(output)
    except OSError:
        if backup is not None:
            backup.rename(output)
        raise
    return backup


def require_separate_output(output, game):
    output, game = Path(output).resolve(), Path(game).resolve()
    if output == game or game in output.parents or output in game.parents:
        raise ValueError("输出目录不能与原始游戏重叠")
    if any((p / ".git").exists() for p in (output, *output.parents)):
        raise ValueError("请将游戏和翻译输出保存在代码仓库之外")
    return output
