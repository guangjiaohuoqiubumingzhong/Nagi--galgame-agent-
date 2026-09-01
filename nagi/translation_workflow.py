"""Three explicitly requested stages for the local translation workbench.

Extraction does not require a model. Translation never implicitly extracts, and
deployment never treats sidecar text as a playable localization.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import uuid4

from .game_launcher import (
    deployment_fingerprint,
    publish_launcher,
    validate_version_launcher,
)
from .translation_storage import (
    game_folder,
    playable_root,
    require_separate_output,
    workflow_root,
)

STAGES = ("extract", "translate", "deploy")
SOURCE_FAMILIES = {
    "qlie": "QLIE",
    "yuris-479": "YU-RIS",
    "kirikiri": "KiriKiri",
    "renpy": "RenPy",
    "tyranoscript": "TyranoScript",
}


class TranslationWorkflow:
    def __init__(self, identifier, game_dir, storage_dir, *, family=None):
        self.workflow_id = identifier
        self.game_dir = str(game_dir)
        self.storage_dir = str(storage_dir)
        self.output_root = str(storage_dir / ("translation-" + identifier))
        self.layout_version = 2 if family else 1
        self.playable_layout_version = 2 if family else 1
        self.engine_family = family
        if family:
            self.output_root = str(
                workflow_root(storage_dir, game_dir, family, identifier)
            )
        self.playable_root = (
            str(playable_root(storage_dir, self.output_root, mode="full"))
            if family
            else self.output_root
        )
        self.locale_settings_path = None
        self.stages = {
            name: {
                "status": "pending",
                "progress": 0,
                "message": "尚未开始",
                "error": None,
            }
            for name in STAGES
        }
        self.active = None
        self.job = None
        self.extraction_job = None
        self.translation_job = None
        self.launcher_path = None
        self.deployment_launcher_path = None
        self.deployment_fingerprint = None
        self.source_kind = None
        self.deployment_support = None
        self.model_identity = None
        self.lock = threading.RLock()

    def public_dict(self):
        with self.lock:
            stages = {key: dict(value) for key, value in self.stages.items()}
            job = self.job.public_dict() if self.job else None
            if self.active and job and self.active != "deploy":
                progress = job["progress"]
                progress = (
                    progress / 0.34
                    if self.active == "extract"
                    else (progress - 0.36) / 0.64
                )
                stages[self.active].update(
                    progress=max(0, min(0.99, progress)), message=job["message"]
                )
            return {
                "workflow_id": self.workflow_id,
                "game_dir": self.game_dir,
                "storage_dir": self.storage_dir,
                "output_root": self.output_root,
                "layout_version": self.layout_version,
                "engine_family": self.engine_family,
                "playable_root": self.playable_root,
                "playable_layout_version": self.playable_layout_version,
                "active_stage": self.active,
                "stages": stages,
                "job": job,
                "mode": self.translation_job.mode if self.translation_job else None,
                "launcher_path": self.launcher_path,
                "deployment_launcher_path": self.deployment_launcher_path,
                "deployment_fingerprint": self.deployment_fingerprint,
                "source_kind": self.source_kind,
                "deployment_support": self.deployment_support,
                "model_identity": self.model_identity,
                "export_dir": self.extraction_job.export_dir
                if self.extraction_job
                else None,
                "corpus_dir": self.extraction_job.corpus_dir
                if self.extraction_job
                else None,
                "translated_scripts_dir": self.translation_job.translated_scripts_dir
                if self.translation_job
                else None,
            }


class TranslationWorkflows:
    def __init__(self, backend, *, deployer=None, state_path=None):
        self.backend = backend
        self.deployer = deployer
        self.items = {}
        self.lock = threading.RLock()
        self.state_path = Path(state_path) if state_path else None

    def checkpoint(self, item):
        if self.state_path is None or item.source_kind not in SOURCE_FAMILIES:
            return
        from .gameio.yuris import save_json

        receipt = Path(item.output_root) / "workflow.json"
        try:
            save_json(receipt, item.public_dict())
            save_json(self.state_path, {"receipt": str(receipt)})
        except OSError as exc:
            item.job.update(event=f"任务恢复记录暂未保存：{exc}")

    def restore(self, receipt):
        """Restore local translation state without starting a thread or API call."""
        receipt = Path(receipt).resolve()
        state = json.loads(receipt.read_text(encoding="utf-8"))
        game, storage, root = (
            Path(state[name]).resolve()
            for name in ("game_dir", "storage_dir", "output_root")
        )
        if (
            state["source_kind"] not in SOURCE_FAMILIES
            or root != receipt.parent
            or storage not in root.parents
            or game == root
            or game in root.parents
            or not game.is_dir()
            or any((p / ".git").exists() for p in (storage, *storage.parents))
        ):
            raise ValueError("Invalid translation recovery directory or engine")
        family = SOURCE_FAMILIES[state["source_kind"]]
        organized = state.get("layout_version") == 2
        if organized:
            if root.parent != storage / family / game_folder(game):
                raise ValueError("Invalid engine-organized recovery path")
            require_separate_output(playable_root(storage, root), game)
        elif root.parent != storage:
            raise ValueError("Invalid legacy recovery directory")

        def child(value):
            path = Path(value).resolve()
            if root not in path.parents:
                raise ValueError("Recovery output escapes the saved workflow directory")
            return str(path)

        item = TranslationWorkflow(state["workflow_id"], game, storage)
        item.output_root, item.source_kind = str(root), state["source_kind"]
        item.layout_version = 2 if organized else 1
        item.engine_family = family if organized else state.get("engine_family")
        item.deployment_support = state.get("deployment_support")
        item.playable_layout_version = state.get("playable_layout_version", 1)
        if item.playable_layout_version not in {1, 2} or (not organized and item.playable_layout_version == 2):
            raise ValueError("Invalid playable layout version")
        item.playable_root = (
            str(playable_root(storage, root, mode=(state.get("mode") or "full")
                              if item.playable_layout_version == 2 else None)) if organized else str(root)
        )
        item.stages = {name: dict(state["stages"][name]) for name in STAGES}
        for stage in item.stages.values():
            if stage["status"] == "running":
                stage.update(
                    status="cancelled",
                    message="后端已重启，已保存的结果可继续使用",
                    error=None,
                )
        item.extraction_job = self.backend.TranslationJob(
            "restored-extraction", str(game), "full", str(root)
        )
        item.extraction_job.update(
            export_dir=child(state["export_dir"]), corpus_dir=child(state["corpus_dir"])
        )
        if family in {"YU-RIS", "KiriKiri", "RenPy", "TyranoScript"}:
            extraction = json.loads(
                (Path(item.extraction_job.corpus_dir) / "extraction.json").read_text(encoding="utf-8")
            )
        else:
            report = json.loads((Path(item.extraction_job.corpus_dir) / "parse-report.json").read_text(encoding="utf-8"))
            extraction = {"text_count": report["summary"]["translatable_count"]}
        item.extraction_job.update(total_units=extraction["text_count"],
                                   character_count=extraction.get("character_count", 0))
        item.job = item.extraction_job
        item.model_identity = state.get("model_identity")
        if state.get("mode"):
            if state["mode"] not in {"full", "partial"} | ({"pilot"} if family == "QLIE" else set()):
                raise ValueError("Invalid saved translation scope")
            saved = state["job"]
            output = child(saved["output_root"])
            job = self.backend.TranslationJob(
                saved["job_id"], str(game), state["mode"], output
            )
            for key in (
                "completed_batches",
                "total_batches",
                "translated_units",
                "total_units",
                "progress",
                "events",
                "character_count",
                "character_glossary_version",
            ):
                setattr(job, key, saved.get(key, getattr(job, key)))
            job.status = item.stages["translate"]["status"]
            job.message = item.stages["translate"]["message"]
            if state.get("translated_scripts_dir"):
                job.translated_scripts_dir = child(state["translated_scripts_dir"])
            if saved.get("preview_dir"):
                job.preview_dir = child(saved["preview_dir"])
            if saved.get("character_glossary_path"):
                from .translation.characters import load_glossary

                job.character_glossary_path = child(saved["character_glossary_path"])
                load_glossary(job.character_glossary_path, expected_version=job.character_glossary_version)
            if not item.model_identity and family == "YU-RIS":
                # Upgrade earlier receipts that recorded provider/model in the plan.
                plan = json.loads(
                    (Path(output) / "translation-plan.json").read_text(encoding="utf-8")
                )
                item.model_identity = {key: plan[key] for key in ("provider", "model")}
            item.translation_job = item.job = job
        if state.get("launcher_path"):
            launcher = Path(state["launcher_path"]).resolve()
            if state.get("deployment_launcher_path"):
                target = Path(state["deployment_launcher_path"]).resolve()
                if not organized or Path(item.playable_root) not in target.parents:
                    raise ValueError("Launcher escapes the saved playable directory")
                game_root = Path(item.playable_root).parent
                validate_version_launcher(game_root, target, state["mode"], state["launcher_path"])
                item.deployment_launcher_path = str(target)
                # Older receipts may contain default_launcher_path. It is no
                # longer used; only the validated test/formal entry is restored.
                fingerprint = state.get("deployment_fingerprint")
                if fingerprint and deployment_fingerprint(target) != fingerprint:
                    raise ValueError("该可玩位置已更新为其他版本，旧版本保存在 playable/.history 中")
                item.deployment_fingerprint = fingerprint
            elif Path(item.playable_root).resolve() not in launcher.parents:
                raise ValueError("Launcher escapes the saved playable directory")
            item.launcher_path = str(launcher)
        with self.lock:
            if any(work.active for work in self.items.values()):
                raise ValueError("Cannot restore while a workflow is active")
            self.items[item.workflow_id] = item
        return item

    def latest(self):
        with self.lock:
            item = next(reversed(self.items.values()), None) if self.items else None
        return item.public_dict() if item else None

    def get(self, identifier):
        with self.lock:
            item = self.items.get(identifier)
        if not item:
            raise ValueError("任务不存在，请先完成第一步：提取资源。")
        return item

    def create(self, game_dir, storage_dir, *, reuse_trial=False):
        if not isinstance(game_dir, str) or not game_dir.strip():
            raise ValueError("请选择原始游戏目录。")
        if not isinstance(storage_dir, str) or not storage_dir.strip():
            raise ValueError("请选择提取资源的保存目录。")
        game, storage = (
            Path(value).expanduser().resolve() for value in (game_dir, storage_dir)
        )
        if not game.is_dir():
            raise ValueError("原始游戏目录不存在。")
        if not storage.is_dir():
            raise ValueError("保存目录不存在，请先创建或选择一个已有目录。")
        if game == storage or game in storage.parents:
            raise ValueError("保存目录不能位于原始游戏目录内，请选择独立目录。")
        if any((parent / ".git").exists() for parent in (storage, *storage.parents)):
            raise ValueError("请将资源保存到代码仓库之外的独立目录。")
        with self.lock:
            if any(item.active for item in self.items.values()):
                raise ValueError("已有翻译流程正在执行，请完成或停止后再提取。")
            from .gameio.text_engines import SOURCE_KINDS, detect_engine
            from .gameio.yuris import is_yuris

            detected = None if reuse_trial or is_yuris(game) else detect_engine(game)
            family = "YU-RIS" if reuse_trial or is_yuris(game) else SOURCE_KINDS.get(detected, "QLIE")
            item = TranslationWorkflow(uuid4().hex[:12], game, storage, family=family)
            require_separate_output(item.output_root, game)
            require_separate_output(item.playable_root, game)
            item.reuse_trial = reuse_trial
            self.items[item.workflow_id] = item
            self.start(item.workflow_id, "extract")
        return item

    def start(self, identifier, stage, *, mode="pilot", confirmed=False):
        if stage not in STAGES:
            raise ValueError("未知执行步骤。")
        item = self.get(identifier)
        with item.lock:
            if item.active:
                raise ValueError("当前步骤仍在执行，请等待完成或安全停止。")
            if stage != "extract" and item.stages["extract"]["status"] != "completed":
                raise ValueError("请先完成第一步：提取资源，再执行后续操作。")
            if stage == "deploy" and item.stages["translate"]["status"] != "completed":
                raise ValueError("请先完成第二步：开始翻译。")
            if stage == "extract" and item.stages["extract"]["status"] == "completed":
                raise ValueError("资源已提取；如需重新提取，请新建任务。")
            if stage == "deploy" and item.stages["deploy"]["status"] == "completed":
                return item
            if stage == "deploy" and item.layout_version == 2:
                item.playable_layout_version = 2
                item.playable_root = str(playable_root(item.storage_dir, item.output_root, mode=item.translation_job.mode))
            config = None
            if stage == "translate":
                if item.source_kind and item.source_kind not in SOURCE_FAMILIES:
                    raise ValueError(
                        "这是已完成的 29 条开场试译，仅支持复用并部署；不支持更改范围或全文翻译。"
                    )
                if mode not in {"pilot", "partial", "full"}:
                    raise ValueError("请选择部分翻译（开场剧情前 50 条）或全文翻译。")
                if item.source_kind == "yuris-479" and mode == "pilot":
                    # Keep older clients' default requests compatible. The new
                    # partial mode must never be silently promoted to full.
                    mode = "full"
                elif item.source_kind in {"kirikiri", "renpy", "tyranoscript"} and mode == "pilot":
                    mode = "partial"
                if confirmed is not True:
                    raise ValueError("请先勾选并确认翻译 API 费用。")
                previous = item.translation_job
                reusable = (
                    previous
                    and previous.mode == mode
                    and item.stages["translate"]["status"] in {"failed", "cancelled"}
                )
                if reusable:
                    item.job = previous
                    if item.job.model_config is None:
                        config = self.backend._configured_model()
                        if any(
                            config.get(key) != value
                            for key, value in (item.model_identity or {}).items()
                        ):
                            raise ValueError(
                                "当前模型服务与原任务不同，请切回原配置后继续"
                            )
                        if not config.get("api_key"):
                            raise ValueError("请先配置原任务的模型 API 密钥")
                        item.job.model_config = config
                    item.job.cancel_event.clear()
                    # Preserve the model/provider of an interrupted run.
                else:
                    config = self.backend._configured_model()
                    if not config.get("api_key"):
                        raise ValueError("请先在设置 → 模型中配置并选择模型。")
                    item.job = self.backend.TranslationJob(
                        uuid4().hex[:12],
                        item.game_dir,
                        mode,
                        str(Path(item.output_root) / (mode + "-" + uuid4().hex[:8])),
                        model_config=config,
                    )
                item.translation_job = item.job
                item.model_identity = {
                    key: item.job.model_config.get(key)
                    for key in ("provider", "model", "base_url", "protocol")
                }
                item.stages["deploy"] = {
                    "status": "pending",
                    "progress": 0,
                    "message": "等待本次翻译完成",
                    "error": None,
                }
                item.launcher_path = None
                item.deployment_launcher_path = None
                item.deployment_fingerprint = None
                if item.layout_version == 2:
                    item.playable_layout_version = 2
                    item.playable_root = str(playable_root(item.storage_dir, item.output_root, mode=mode))
            elif stage == "extract":
                if (
                    item.extraction_job
                    and not item.extraction_job.review_path
                    and item.stages["extract"]["status"] == "failed"
                ):
                    # Preserve partial outputs, and retry into a fresh folder.
                    item.output_root = str(
                        Path(item.output_root).parent
                        / ("translation-" + uuid4().hex[:12])
                    )
                    if item.layout_version == 2:
                        item.playable_root = str(
                            playable_root(item.storage_dir, item.output_root, mode="full")
                        )
                    item.extraction_job = None
                if item.extraction_job is None:
                    item.extraction_job = self.backend.TranslationJob(
                        uuid4().hex[:12], item.game_dir, "pilot", item.output_root
                    )
                item.job = item.extraction_job
                item.job.cancel_event.clear()
            item.active = stage
            item.stages[stage].update(
                status="running", progress=0, message="正在准备执行", error=None
            )
            if stage != "deploy":
                item.job.update(
                    status="running",
                    progress=0 if stage == "extract" else 0.36,
                    message="正在准备执行",
                    error_code=None,
                )
            self.checkpoint(item)
            threading.Thread(
                target=self._run,
                args=(item, stage),
                daemon=True,
                name=f"translation-{identifier}-{stage}",
            ).start()
        return item

    def cancel(self, identifier):
        item = self.get(identifier)
        with item.lock:
            if item.active in {"extract", "translate"}:
                item.job.cancel_event.set()
                item.job.update(message="已请求停止，将在当前安全步骤结束后停止")
            elif item.active == "deploy":
                raise ValueError("正在发布启动文件，请等待本步骤结束。")
        return item

    def _run(self, item, stage):
        try:
            job = item.job
            if stage == "extract":
                if job.cancel_event.is_set():
                    raise InterruptedError()
                if getattr(item, "reuse_trial", False):
                    from .gameio.deployment import restore_trial

                    restore_trial(item, self.backend.TranslationJob)
                    with item.lock:
                        item.active = None
                    return
                from .gameio.yuris import extract_game, is_yuris

                if is_yuris(item.game_dir):
                    export, corpus = extract_game(item.game_dir, item.output_root, job)
                    item.source_kind = "yuris-479"
                    self._finish(
                        item,
                        stage,
                        "completed",
                        f"YU-RIS 已提取 {job.total_units} 条文本；可在第二步选择翻译范围",
                    )
                    return
                from .gameio.text_engines import SOURCE_KINDS, detect_engine
                from .gameio.text_engines import extract_game as extract_text_game

                text_engine = detect_engine(item.game_dir)
                if text_engine:
                    export, corpus = extract_text_game(
                        item.game_dir, item.output_root, job, text_engine
                    )
                    item.source_kind = text_engine
                    item.engine_family = SOURCE_KINDS[text_engine]
                    item.deployment_support = {
                        "supported": True,
                        "profile": f"{text_engine}-text-script-v1",
                        "message": f"已适配 {SOURCE_KINDS[text_engine]} 文本脚本；翻译后可生成独立汉化副本",
                    }
                    self._finish(
                        item,
                        stage,
                        "completed",
                        f"{SOURCE_KINDS[text_engine]} 已提取 {job.total_units} 条文本；可选择部分或全文翻译",
                    )
                    return
                assessment = self.backend.assess_game_directory(item.game_dir)
                if not assessment.get("supported_archive_count") or not assessment.get(
                    "key_available"
                ):
                    raise ValueError(
                        "未找到受支持的 QLIE、YU-RIS 479、KiriKiri、Ren'Py 或 TyranoScript 资源。"
                    )
                # This stage must publish inside the explicitly chosen location.
                assessment = {**assessment, "prepared": None}
                export, corpus = self.backend._prepare_source(job, assessment)
                if not export or not corpus:
                    self._finish(item, stage, job.status, job.message)
                    return
                if job.cancel_event.is_set():
                    raise InterruptedError()
                job.update(
                    export_dir=str(export), corpus_dir=str(corpus), progress=0.34
                )
                item.source_kind = "qlie"
                from .gameio.qlie.deployment import executable_profile

                try:
                    _, _, profile = executable_profile(item.game_dir)
                    item.deployment_support = {"supported": True, "profile": profile,
                                               "message": "已适配 QLIE 3.1：翻译完成后可生成独立汉化副本"}
                except ValueError as exc:
                    item.deployment_support = {"supported": False, "message": str(exc)}
                self._finish(
                    item,
                    stage,
                    "completed",
                    "提取完成，脚本和语料已保存；可以开始翻译。",
                )
            elif stage == "translate":
                source = item.extraction_job
                if (
                    not Path(source.export_dir).is_dir()
                    or not Path(source.corpus_dir).is_dir()
                ):
                    raise ValueError("第一步的提取结果已移动或删除，请重新提取。")
                if item.source_kind == "yuris-479":
                    from .translation.yuris import translate

                    translate(
                        job,
                        Path(source.corpus_dir),
                        lambda: self.backend._model_client(job.model_config),
                    )
                elif item.source_kind in {"kirikiri", "renpy", "tyranoscript"}:
                    from .translation.text_engines import translate

                    translate(
                        job,
                        Path(source.corpus_dir),
                        lambda: self.backend._model_client(job.model_config),
                        item.source_kind,
                    )
                else:
                    self.backend._translate_and_publish(
                        job, Path(source.export_dir), Path(source.corpus_dir)
                    )
                self._finish(item, stage, job.status, job.message)
            else:
                if self.deployer is None:
                    raise ValueError(
                        "当前引擎的部署适配器尚未接入。译文已保留，但不能生成可游玩的汉化启动文件。"
                    )

                def progress(value, message):
                    with item.lock:
                        item.stages[stage].update(
                            progress=max(0, min(0.99, value)), message=message
                        )

                if hasattr(self.backend, "_locale_settings"):
                    item.locale_settings_path = str(
                        self.backend._locale_settings().path
                    )
                launcher = Path(self.deployer(item, progress)).resolve()
                if not launcher.is_file() or not any(
                    Path(p).resolve() in launcher.parents
                    for p in (item.output_root, item.playable_root)
                ):
                    raise ValueError("部署未产生有效的独立启动文件。")
                if item.layout_version == 2:
                    if Path(item.playable_root).resolve() not in launcher.parents:
                        raise ValueError("引擎启动文件必须生成在本次 playable 版本目录中")
                    version = publish_launcher(
                        Path(item.playable_root).parent, launcher, item.translation_job.mode,
                    )
                    item.deployment_launcher_path = str(launcher)
                    item.deployment_fingerprint = deployment_fingerprint(launcher)
                    item.launcher_path = str(version)
                else:
                    item.launcher_path = str(launcher)
                self._finish(item, stage, "completed", "汉化启动文件已生成。")
        except InterruptedError:
            self._finish(item, stage, "cancelled", "本步骤已安全停止，可继续执行。")
        except Exception as exc:  # noqa: BLE001 - persist worker failures for the UI
            self._finish(item, stage, "failed", str(exc))

    def _finish(self, item, stage, status, message):
        if status not in {"completed", "failed", "cancelled", "review_required"}:
            status, message = "failed", "执行没有产生完整结果，请检查本步骤后重试。"
        with item.lock:
            values = item.stages[stage]
            if stage != "deploy" and item.job:
                current = item.job.progress
                current = (
                    current / 0.34 if stage == "extract" else (current - 0.36) / 0.64
                )
                values["progress"] = max(0, min(0.99, current))
            values.update(
                status=status,
                message=message,
                error=message if status in {"failed", "review_required"} else None,
            )
            if status == "completed":
                values["progress"] = 1
            if stage != "deploy" and item.job:
                item.job.update(status=status, message=message, event=message)
            item.active = None
            self.checkpoint(item)
