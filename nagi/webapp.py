"""Local-only web console for the guarded QLIE translation workflow.

The server binds to loopback, never returns provider secrets, and keeps game
archives read-only.  Translation output is published to a new sidecar tree;
pack deployment remains an explicit, separately evaluated step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from . import __version__
from .paths import APP_ID, application_root, data_root, state_root, resource_root, translation_root, ensure_data_directory
from .agent_web import AgentWebService
from .launcher import installation_id
from .release_info import capabilities, acknowledge
from .config import load_project_env, provider_env
from .directory_picker import choose_directory as _choose_directory
from .gameio.deployment import deploy_workflow
from .gameio.qlie import (
    apply_qlie_corpus_plan,
    apply_script_export_plan,
    build_qlie_corpus_plan,
    build_script_export_plan,
    inspect_filepack_toc,
    inspect_game_directory,
    read_filepack_entry,
    write_unknown_review_template,
)
from .gameio.qlie.pe import (
    PeResourceError,
    load_icon_key_from_pe,
    load_reskey_from_pe,
)
from .locale_emulator import LocaleEmulatorSettings
from .model_settings import ModelSettings, client_from_config
from .rag.semantic import RetrievalCache, local_models, model_status
from .translation import (
    TranslationAdjacentContext,
    TranslationPreviewSpec,
    apply_translation_preview,
    build_translation_patch_preview,
    build_translation_run_spec,
    execute_translation_run_next,
    initialize_translation_run,
    load_translation_candidates,
    load_translation_checkpoint,
    publish_translation_preview,
)
from .translation.cache import _write_json_atomic
from .translation.context import TranslationContextConfig, prepare_translation_requests
from .translation_storage import workflow_root
from .translation_workflow import TranslationWorkflows

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_NEW_TOKENS = 8192
MAX_BATCH_ATTEMPTS_PER_JOB = 3
PROMPT_VERSION = "qlie-translation-v1"
TERMINOLOGY_VERSION = "none"
TARGET_LANGUAGE = "zh-CN"
TERMINAL_JOB_STATUSES = {
    "completed",
    "cancelled",
    "failed",
    "review_required",
}


def _project_root() -> Path:
    return application_root()


def _asset_root() -> Path:
    return resource_root() / "web_ui"


def _artifact_root() -> Path:
    override = (os.environ.get("NAGI_TRANSLATION_OUTPUT_ROOT") or os.environ.get("NAGI_QLIE_WEB_OUTPUT_ROOT", "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    return translation_root()


def _same_path(left, right) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(Path(right).resolve())
        )
    except (OSError, TypeError, ValueError):
        return False


def _read_json(path: Path, *, max_bytes: int = 16 * 1024 * 1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError(f"unsafe or oversized JSON file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _legacy_model_config() -> dict:
    load_project_env(data_root(), search_parents=False)
    model = provider_env("NAGI_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",), "deepseek-v4-pro")
    base_url = provider_env(
        "NAGI_DEEPSEEK_API_BASE",
        ("DEEPSEEK_API_BASE",),
        "https://api.deepseek.com/anthropic",
    )
    api_key = provider_env("NAGI_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
    return {
        "provider": "deepseek",
        "model": model,
        "base_url": base_url,
        "api_key_configured": bool(api_key),
        "api_key": api_key,
    }


def _model_settings():
    return ModelSettings(state_root() / "web" / "model-providers.json", _legacy_model_config)


def _locale_settings():
    return LocaleEmulatorSettings(state_root() / "web" / "locale-emulator.json")


def _configured_model() -> dict:
    return _model_settings().active_config()


def _model_client(config=None):
    return client_from_config(config if config is not None else _configured_model(), translation=True)


def _agent_model_client():
    return client_from_config(_configured_model())


def _find_game_exe(
    game_dir: Path,
    versions=(),
    archives=(),
) -> tuple[Path | None, list[str]]:
    warnings = []
    candidates = sorted(
        (
            path
            for path in game_dir.glob("*.exe")
            if path.is_file() and "uninstall" not in path.name.casefold()
        ),
        key=lambda path: (
            "setting" in path.name.casefold(),
            -path.stat().st_size,
            path.name.casefold(),
        ),
    )
    required_loaders = []
    if "3.0" in versions:
        required_loaders.append(load_icon_key_from_pe)
    if "3.1" in versions:
        required_loaders.append(load_reskey_from_pe)
    if not required_loaders:
        return None, warnings

    key_probes = []
    for version in ("3.0", "3.1"):
        if version not in versions:
            continue
        for archive in archives:
            if archive.format_version != version:
                continue
            archive_path = game_dir / archive.relative_path
            report = inspect_filepack_toc(archive_path)
            entry = next(
                (
                    item
                    for item in report.entries
                    if item.obfuscation_flag
                    and item.compression_flag == 1
                    and "pack_keyfile" not in item.internal_path.casefold()
                ),
                None,
            )
            if entry is not None:
                key_probes.append((archive_path, entry.index))
                break
    for path in candidates:
        try:
            for loader in required_loaders:
                loader(path)
            for archive_path, entry_index in key_probes:
                probe = read_filepack_entry(
                    archive_path,
                    entry_index=entry_index,
                    exe_path=path,
                )
                if probe.report.status != "supported":
                    raise PeResourceError(
                        "key_unavailable",
                        "version-specific QLIE key probe failed",
                    )
            return path, warnings
        except PeResourceError as exc:
            warnings.append(f"{path.name}: {exc}")
        except OSError as exc:
            warnings.append(f"{path.name}: {exc}")
    return None, warnings


def _prepared_workspace(game_dir: Path) -> dict | None:
    parent = _project_root().parent
    exports = []
    manifests = [*parent.glob("qlie-script-export*/manifest.json"),
                 *_artifact_root().glob("QLIE/*/*/script-export/manifest.json")]
    for manifest_path in sorted(manifests):
        try:
            manifest = _read_json(manifest_path, max_bytes=64 * 1024 * 1024)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if _same_path(manifest.get("game_dir"), game_dir):
            exports.append(manifest_path.parent)
    for export_dir in exports:
        reports = [*parent.glob("qlie-corpus*/parse-report.json"),
                   *_artifact_root().glob("QLIE/*/*/corpus/parse-report.json")]
        for report_path in sorted(reports):
            try:
                report = _read_json(report_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if report.get("status") != "published" or not _same_path(
                report.get("source_dir"), export_dir
            ):
                continue
            summary = report.get("summary", {})
            return {
                "export_dir": str(export_dir),
                "corpus_dir": str(report_path.parent),
                "segment_count": int(summary.get("segment_count", 0)),
                "translatable_count": int(summary.get("translatable_count", 0)),
                "unknown_count": int(summary.get("unknown_count", 0)),
            }
    return None


def assess_game_directory(value) -> dict:
    game_dir = Path(value).expanduser().resolve()
    inspection = inspect_game_directory(game_dir, hash_files=False)
    supported = [item for item in inspection.archives if item.status == "supported"]
    versions = sorted(
        {item.format_version for item in supported if item.format_version}
    )
    exe_path, exe_warnings = _find_game_exe(game_dir, versions, supported)
    v30_key_available = "3.0" not in versions or any(
        path.is_file() and not path.is_symlink()
        for path in (
            game_dir / "key.fkey",
            game_dir / "DLL" / "key.fkey",
            game_dir / "GameData" / "key.fkey",
        )
    )
    numbered_packs = []
    for item in supported:
        stem = Path(item.relative_path).stem.casefold()
        if stem.startswith("data") and stem[4:].isdigit():
            numbered_packs.append(int(stem[4:]))
    version_path = game_dir / "version.txt"
    patch_slots = 0
    if version_path.is_file() and version_path.stat().st_size <= 64 * 1024:
        raw_version = version_path.read_bytes()
        encoding = "utf-16-le" if raw_version[:256].count(b"\x00") > 16 else "utf-8-sig"
        text = raw_version.decode(encoding, errors="replace").lstrip("\ufeff")
        patch_slots = sum(
            line.strip().casefold().startswith("patch") and "=" in line
            for line in text.splitlines()
        )
    prepared = _prepared_workspace(game_dir)
    filepack31_only = bool(versions) and set(versions) == {"3.1"}
    patch_candidate = filepack31_only and bool(numbered_packs)
    return {
        "game_dir": str(game_dir),
        "status": inspection.status,
        "archive_count": len(inspection.archives),
        "supported_archive_count": len(supported),
        "versions": versions,
        "exe_path": str(exe_path) if exe_path else None,
        "key_available": (
            set(versions).issubset({"1.0"})
            or (exe_path is not None and v30_key_available)
        ),
        "warnings": list(inspection.warnings) + exe_warnings[:4],
        "prepared": prepared,
        "pack_assessment": {
            "patch_pack_candidate": patch_candidate,
            "confidence": "medium" if patch_candidate else "low",
            "highest_data_pack": max(numbered_packs) if numbered_packs else None,
            "existing_extra_pack": any(number > 9 for number in numbered_packs),
            "version_patch_slots": patch_slots,
            "recommended_strategy": "new_patch_pack"
            if patch_candidate
            else "sidecar_only",
            "direct_overwrite_recommended": False,
            "reasons": [
                "FilePackVer3.1 has a documented community repacker"
                if filepack31_only
                else "the current archive set is not eligible for the 3.1 patch-pack path",
                "the game already contains a high-numbered data pack"
                if any(number > 9 for number in numbered_packs)
                else "no existing high-numbered patch pack was found",
                "Chinese glyph rendering and script encoding still require an in-game pilot test",
            ],
        },
    }


@dataclass
class TranslationJob:
    job_id: str
    game_dir: str
    mode: str
    output_root: str
    status: str = "queued"
    stage: str = "preflight"
    stage_index: int = 0
    stage_count: int = 5
    progress: float = 0.0
    message: str = "任务已排队"
    completed_batches: int = 0
    total_batches: int = 0
    translated_units: int = 0
    total_units: int = 0
    review_path: str | None = None
    export_dir: str | None = None
    corpus_dir: str | None = None
    translated_scripts_dir: str | None = None
    preview_dir: str | None = None
    error_code: str | None = None
    events: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    model_config: dict | None = field(default=None, repr=False)
    context_config: TranslationContextConfig = field(default_factory=TranslationContextConfig, repr=False)
    context_summary: dict = field(default_factory=dict)
    retrieval_models: tuple | None = field(default=None, repr=False)
    character_count: int = 0
    character_glossary_version: str | None = None
    character_glossary_path: str | None = None

    def update(self, *, event=None, **values):
        with self.lock:
            for name, value in values.items():
                setattr(self, name, value)
            self.updated_at = time.time()
            if event:
                self.events.append(
                    {
                        "time": datetime.now(timezone.utc)
                        .astimezone()
                        .strftime("%H:%M:%S"),
                        "message": str(event),
                    }
                )
                self.events[:] = self.events[-80:]

    def public_dict(self):
        with self.lock:
            return {
                "job_id": self.job_id,
                "game_dir": self.game_dir,
                "mode": self.mode,
                "output_root": self.output_root,
                "status": self.status,
                "stage": self.stage,
                "stage_index": self.stage_index,
                "stage_count": self.stage_count,
                "progress": round(self.progress, 4),
                "message": self.message,
                "completed_batches": self.completed_batches,
                "total_batches": self.total_batches,
                "translated_units": self.translated_units,
                "total_units": self.total_units,
                "review_path": self.review_path,
                "export_dir": self.export_dir,
                "corpus_dir": self.corpus_dir,
                "translated_scripts_dir": self.translated_scripts_dir,
                "preview_dir": self.preview_dir,
                "error_code": self.error_code,
                "events": list(self.events),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "context_summary": dict(self.context_summary),
                "character_count": self.character_count,
                "character_glossary_version": self.character_glossary_version,
                "character_glossary_path": self.character_glossary_path,
            }


class JobRegistry:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.RLock()

    def create(self, game_dir, mode):
        job_id = uuid4().hex[:12]
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        output_root = workflow_root(_artifact_root(), game_dir, "QLIE", f"{timestamp}-{job_id}")
        job = TranslationJob(job_id, str(game_dir), mode, str(output_root))
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            return [job.public_dict() for job in self._jobs.values()]


JOBS = JobRegistry()


def _build_adjacent_context(plan):
    units = [unit for batch in plan.batches for unit in batch.units]
    by_segment = {unit.segment_id: unit for unit in units}
    return {
        unit.unit_id: TranslationAdjacentContext(
            previous_text=(
                by_segment[unit.previous_segment_id].source_text
                if unit.previous_segment_id in by_segment
                else None
            ),
            next_text=(
                by_segment[unit.next_segment_id].source_text
                if unit.next_segment_id in by_segment
                else None
            ),
        )
        for unit in units
    }


def _prepare_source(job: TranslationJob, assessment: dict):
    from .gameio.characters import load_qlie_characters

    def remember_characters(corpus_dir):
        characters = load_qlie_characters(corpus_dir)
        job.update(character_count=len(characters["characters"]))

    if job.cancel_event.is_set():
        raise InterruptedError("source preparation cancelled")
    if job.export_dir and Path(job.export_dir).is_dir():
        export_dir = Path(job.export_dir)
        if job.corpus_dir and Path(job.corpus_dir).is_dir():
            remember_characters(Path(job.corpus_dir))
            return export_dir, Path(job.corpus_dir)
        if job.review_path:
            corpus_dir = Path(job.output_root) / "corpus"
            corpus_plan = build_qlie_corpus_plan(
                export_dir,
                corpus_dir,
                review_path=job.review_path,
            )
            if corpus_plan.status == "review_required":
                job.update(
                    status="review_required",
                    message="审核文件仍有未填写或不匹配的记录",
                    event="未知行审核尚未通过，请检查 label 字段",
                )
                return None, None
            if corpus_plan.status != "ready":
                raise RuntimeError(
                    f"corpus_plan:{corpus_plan.status}:{corpus_plan.reason}"
                )
            result = apply_qlie_corpus_plan(corpus_plan)
            if result.status != "published":
                raise RuntimeError(f"corpus:{result.status}:{result.reason}")
            job.update(corpus_dir=str(corpus_dir), event="审核通过，语料构建完成")
            remember_characters(corpus_dir)
            return export_dir, corpus_dir

    prepared = assessment.get("prepared")
    if prepared:
        job.update(
            event="检测到已发布的脚本语料，跳过重复提取",
            stage="corpus",
            stage_index=2,
            progress=0.32,
            message="复用已验证语料",
            export_dir=prepared["export_dir"],
            corpus_dir=prepared["corpus_dir"],
        )
        remember_characters(Path(prepared["corpus_dir"]))
        return Path(prepared["export_dir"]), Path(prepared["corpus_dir"])

    root = Path(job.output_root)
    export_dir = root / "script-export"
    corpus_dir = root / "corpus"
    review_path = root / "review" / "unknown-review.jsonl"
    root.mkdir(parents=True, exist_ok=False)
    job.update(event="开始事务导出脚本", stage="export", stage_index=1, progress=0.12)
    plan = build_script_export_plan(
        job.game_dir, export_dir, conflict_policy="preserve"
    )
    # This verified QLIE profile loads numbered packs in ascending order.
    # Export the effective layer so partial translation targets the script the
    # game actually executes, not an older copy shadowed by data8/data10.
    from .gameio.qlie.deployment import executable_profile

    try:
        executable_profile(job.game_dir)
    except ValueError:
        pass
    else:
        plan = build_script_export_plan(
            job.game_dir, export_dir, archives=list(plan.archive_order),
            conflict_policy="precedence",
        )
    if plan.status != "ready":
        raise RuntimeError(f"export_plan:{plan.status}:{plan.reason}")
    result = apply_script_export_plan(plan, exe_path=assessment["exe_path"])
    if result.status != "exported":
        raise RuntimeError(f"export:{result.status}:{result.reason}")
    job.update(
        event="脚本导出完成，正在构建语料",
        stage="corpus",
        stage_index=2,
        progress=0.24,
        export_dir=str(export_dir),
    )
    if job.cancel_event.is_set():
        raise InterruptedError("source preparation cancelled")

    corpus_plan = build_qlie_corpus_plan(export_dir, corpus_dir)
    if corpus_plan.status == "review_required":
        write_unknown_review_template(corpus_plan, review_path)
        job.update(
            status="review_required",
            review_path=str(review_path),
            progress=0.3,
            message="发现无法自动判断的脚本行，请完成审核后继续",
            event=f"未知行审核模板已生成：{review_path}",
        )
        return None, None
    if corpus_plan.status != "ready":
        raise RuntimeError(f"corpus_plan:{corpus_plan.status}:{corpus_plan.reason}")
    result = apply_qlie_corpus_plan(corpus_plan)
    if result.status != "published":
        raise RuntimeError(f"corpus:{result.status}:{result.reason}")
    job.update(event="语料构建完成", progress=0.34, corpus_dir=str(corpus_dir))
    remember_characters(corpus_dir)
    return export_dir, corpus_dir


def _completed_translation_run(spec):
    candidates = sorted(
        [*_artifact_root().glob("*/translation-run"),
         *_artifact_root().glob("QLIE/*/*/translation-run"),
         *_artifact_root().glob("QLIE/*/*/*/translation-run")],
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for candidate in candidates:
        try:
            checkpoint = load_translation_checkpoint(spec, candidate)
        except (OSError, ValueError):
            continue
        if checkpoint["batches"] and all(
            item["status"] == "completed" for item in checkpoint["batches"]
        ):
            return candidate
    return None


def _translation_model_id(config):
    model = config["model"]
    kind = config.get("provider", "deepseek")
    base = config.get("base_url", "").rstrip("/")
    if kind != "deepseek" or base not in {"", "https://api.deepseek.com", "https://api.deepseek.com/anthropic"}:
        # Keep existing DeepSeek checkpoints compatible; separate other endpoints.
        endpoint_id = hashlib.sha256(base.encode()).hexdigest()[:12]
        model = f"{kind}:{endpoint_id}:{model}"
    return model


def _translate_and_publish(job: TranslationJob, export_dir: Path, corpus_dir: Path):
    from .gameio.characters import load_qlie_characters
    from .translation.characters import prepare_glossary
    from .translation.planner import build_translation_batch_plan

    config = job.model_config or _configured_model()
    model = _translation_model_id(config)
    glossary = None
    opening = None
    opening_path = Path(job.output_root) / "opening-selection.json"
    old_partial = (job.mode == "partial" and (Path(job.output_root) / "translation-run").is_dir()
                   and not opening_path.exists())
    if job.mode == "partial" and not old_partial:
        from .gameio.qlie.opening import select_opening
        from .gameio.yuris import save_json

        opening = select_opening(corpus_dir)
        # Validate every selected ID before the separate paid name preflight.
        scoped = build_translation_batch_plan(corpus_dir, selected_segment_ids=opening["selected_ids"])
        if scoped.status != "ready":
            raise ValueError("开场选取未通过语料校验，未调用 API")
        if opening_path.exists() and json.loads(opening_path.read_text(encoding="utf-8")) != opening:
            raise ValueError("开场选取范围已改变，请新建翻译任务")
        save_json(opening_path, opening)
    legacy = ((Path(job.output_root) / "translation-run").is_dir()
              and not (Path(job.output_root) / "character-policy.json").exists())
    if legacy:
        job.update(event="此旧任务按原请求继续；新建翻译任务才启用自动人物译名")
    else:
        preflight = build_translation_batch_plan(corpus_dir, model_id=model, prompt_version=PROMPT_VERSION)
        if preflight.status != "ready":
            raise ValueError("语料未通过完整性检查，未调用人物名翻译 API")
        characters = load_qlie_characters(corpus_dir)
        glossary = prepare_glossary(
            job, corpus_dir, characters,
            lambda: _model_client(config) if job.model_config is not None else _model_client(),
            config=config,
        )
    limit = {"pilot": DEFAULT_BATCH_SIZE, "partial": 50}.get(job.mode)
    job.update(
        stage="translation",
        stage_index=3,
        progress=0.36,
        message="正在规划翻译批次",
        event="开始构建确定性翻译计划",
    )
    if job.retrieval_models is None:
        job.retrieval_models = local_models()
    embedder, reranker, missing = job.retrieval_models
    mode_label = "BM25 + 本地语义检索" if embedder else "BM25（本地语义模型未安装）"
    job.update(event=f"上下文模式：{mode_label}" + ("，启用本地重排" if reranker else "，未启用神经重排"))
    context_summary = {"missing_models": list(missing)}

    def context_progress(message):
        if job.cancel_event.is_set():
            raise InterruptedError("translation context cancelled")
        job.update(message=message)

    plan, requests, terminology_snapshot = prepare_translation_requests(
        corpus_dir,
        model_id=model,
        prompt_version=PROMPT_VERSION,
        target_language=TARGET_LANGUAGE,
        config=job.context_config,
        batch_size=DEFAULT_BATCH_SIZE,
        limit=limit,
        source_limit=50 if old_partial else None,
        selected_segment_ids=opening["selected_ids"] if opening else (),
        embedder=embedder, reranker=reranker,
        cache=RetrievalCache(state_root() / "rag-cache" / "retrieval.sqlite3"),
        progress=context_progress, summary=context_summary,
        character_glossary=glossary,
    )
    evidence_count = sum(len({e.reference.content_sha256 for unit in request.units for e in unit.rag_context}) for request in requests)
    job.update(context_summary={**context_summary, "index_id": plan.config.rag_index_id,
        "rag_budget_chars_per_batch": job.context_config.rag_budget_chars,
        "evidence_count": evidence_count,
        "batches_with_evidence": sum(any(unit.rag_context for unit in request.units) for request in requests)},
        event=f"本地上下文检索完成：选用 {evidence_count} 条证据，每批最多 {job.context_config.rag_budget_chars} 字符")
    spec = build_translation_run_spec(plan, requests)
    run_dir = Path(job.output_root) / "translation-run"
    if not run_dir.is_dir():
        completed_run = _completed_translation_run(spec)
        if completed_run is not None:
            run_dir = completed_run
            job.update(event="复用已完成的同批次译文，不再重复调用模型")
    if run_dir.is_dir():
        checkpoint = load_translation_checkpoint(spec, run_dir)
        completed_batches = sum(
            item["status"] == "completed" for item in checkpoint["batches"]
        )
        completed_units = sum(
            len(request.units)
            for request, item in zip(spec.requests, checkpoint["batches"])
            if item["status"] == "completed"
        )
        job.update(
            completed_batches=completed_batches,
            translated_units=completed_units,
            event=f"从断点继续：已有 {completed_batches} 个批次完成",
        )
    else:
        initialize_translation_run(spec, run_dir, context_config=job.context_config.to_dict())
    # A private snapshot makes retries reproducible. Never overwrite changed
    # reference material or publish its prose in job status responses.
    context_path = run_dir / "context-config.json"
    snapshot = job.context_config.to_dict()
    if context_path.exists():
        if _read_json(context_path) != snapshot:
            raise ValueError("translation context snapshot changed; start a new run")
    else:
        _write_json_atomic(context_path, snapshot)
    total_units = sum(len(request.units) for request in spec.requests)
    job.update(
        total_batches=len(spec.requests),
        total_units=total_units,
        message=f"正在调用 {config['model']} 翻译",
        event=f"翻译计划包含 {len(spec.requests)} 批、{total_units} 条文本",
    )
    client = _model_client(config) if job.model_config is not None else _model_client()
    consecutive_batch_failures = 0
    while job.completed_batches < len(spec.requests):
        if job.cancel_event.is_set():
            job.update(
                status="cancelled", message="任务已安全停止", event="用户停止了任务"
            )
            return
        result = execute_translation_run_next(
            spec,
            run_dir,
            client,
            max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        )
        if result.failed_batch_count:
            consecutive_batch_failures += 1
            if consecutive_batch_failures < MAX_BATCH_ATTEMPTS_PER_JOB:
                job.update(
                    message="模型输出未通过校验，正在自动重试",
                    event=(
                        "模型响应格式异常，自动重试 "
                        f"{consecutive_batch_failures}/{MAX_BATCH_ATTEMPTS_PER_JOB - 1}"
                    ),
                )
                continue
            raise RuntimeError(
                "translation_batch:模型输出连续未通过严格校验"
                f"（{result.reason}）"
            )
        consecutive_batch_failures = 0
        ordinal = result.completed_batch_count
        completed_units = result.completed_unit_count
        job.update(
            completed_batches=ordinal,
            translated_units=completed_units,
            progress=0.36 + 0.46 * ordinal / len(spec.requests),
            message=f"已完成 {ordinal}/{len(spec.requests)} 个翻译批次",
            event=(
                f"第 {ordinal} 批翻译完成"
                if ordinal <= 3 or ordinal == len(spec.requests) or ordinal % 25 == 0
                else None
            ),
        )

    job.update(
        stage="validation",
        stage_index=4,
        progress=0.84,
        message="正在校验标签、占位符和换行",
        event="全部候选译文已生成，开始结构安全校验",
    )
    candidates = load_translation_candidates(spec, run_dir)
    units = tuple(unit for batch in plan.batches for unit in batch.units)
    if opening and opening["display_ids"] and glossary is not None:
        from .translation.models import build_translation_candidate

        name_plan = build_translation_batch_plan(
            corpus_dir, model_id=model, prompt_version=PROMPT_VERSION,
            terminology_version=plan.config.terminology_version,
            rag_index_id=plan.config.rag_index_id,
            selected_segment_ids=opening["display_ids"],
        )
        if name_plan.status != "ready" or name_plan.segments_sha256 != plan.segments_sha256:
            raise ValueError("人物显示名与开场语料不匹配，停止回填")
        name_units = tuple(unit for batch in name_plan.batches for unit in batch.units)
        name_candidates = tuple(build_translation_candidate(
            unit, unit.source_text, model_id=model, prompt_version=PROMPT_VERSION,
            terminology_version=plan.config.terminology_version, rag_index_id=plan.config.rag_index_id,
        ) for unit in name_units)
        units += name_units
        candidates = tuple(candidates) + name_candidates
    if glossary is not None:
        candidates = glossary.bind_qlie_candidates(units, candidates)
    preview_spec = TranslationPreviewSpec(
        run_id=spec.run_id,
        plan_id=spec.plan_id,
        corpus_dir=spec.corpus_dir,
        segments_sha256=spec.segments_sha256,
        parse_report_sha256=spec.parse_report_sha256,
    )
    preview = build_translation_patch_preview(
        units,
        candidates,
        preview_spec,
        current_segments_sha256=spec.segments_sha256,
        current_parse_report_sha256=spec.parse_report_sha256,
        current_source_hashes={
            unit.unit_id: unit.source.source_sha256 for unit in units
        },
        expected_candidate_hashes={
            candidate.unit_id: candidate.translated_text_sha256
            for candidate in candidates
        },
        terminology_snapshot=terminology_snapshot,
        max_line_chars=60,
    )
    if not preview.patch_eligible:
        raise RuntimeError("validation:translated text failed structural safety checks")
    preview_dir = Path(job.output_root) / "translation-preview"
    translated_dir = Path(job.output_root) / "translated-scripts"
    publish_translation_preview(
        preview,
        preview_dir,
        include_text=True,
        forbidden_roots=(job.game_dir,),
    )
    apply_translation_preview(
        preview_dir,
        export_dir,
        translated_dir,
        approved=True,
        dry_run=False,
        forbidden_roots=(job.game_dir,),
        fallback_encoding="utf-16-le-bom",
    )
    job.update(
        status="completed",
        stage="output",
        stage_index=5,
        progress=1.0,
        message="翻译脚本已安全发布",
        preview_dir=str(preview_dir),
        translated_scripts_dir=str(translated_dir),
        event="独立译文脚本目录已生成；原始游戏未被修改",
    )


def _run_job(job: TranslationJob):
    try:
        job.update(status="running", message="正在检查游戏目录", event="开始游戏预检")
        assessment = assess_game_directory(job.game_dir)
        if assessment["status"] not in {"supported", "partial"}:
            raise RuntimeError("preflight:no supported QLIE archives were found")
        if not assessment["key_available"]:
            raise RuntimeError(
                "preflight:version-specific QLIE EXE/key.fkey material was not found"
            )
        job.update(
            progress=0.08,
            event=f"识别到 {assessment['supported_archive_count']} 个 QLIE 资源包",
        )
        export_dir, corpus_dir = _prepare_source(job, assessment)
        if export_dir is None:
            return
        _translate_and_publish(job, export_dir, corpus_dir)
    except InterruptedError:
        job.update(status="cancelled", message="任务已安全停止", event="上下文准备已停止")
    except Exception as exc:  # noqa: BLE001 - worker boundary must persist unexpected failures
        code, _, reason = str(exc).partition(":")
        job.update(
            status="failed",
            error_code=code or type(exc).__name__,
            message=reason or str(exc),
            event=f"任务停止：{reason or str(exc)}",
        )
        debug_path = Path(job.output_root).parent / f"{job.job_id}-error.log"
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass


def start_translation_job(game_dir, mode="pilot"):
    path = Path(game_dir).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("game directory does not exist")
    if mode not in {"pilot", "partial", "full"}:
        raise ValueError("mode must be partial or full")
    config = _configured_model()
    if not config.get("api_key"):
        raise ValueError("请先在设置 → 模型中配置并选择模型")
    job = JOBS.create(path, mode)
    job.model_config = config
    thread = threading.Thread(
        target=_run_job, args=(job,), daemon=True, name=f"qlie-{job.job_id}"
    )
    thread.start()
    return job


def resume_translation_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        raise ValueError("job not found")
    if job.status not in {"cancelled", "failed", "review_required"}:
        raise ValueError("only stopped, failed, or review-required jobs can resume")
    job.cancel_event.clear()
    job.update(
        status="queued",
        error_code=None,
        message="正在从已有结果继续",
        event="用户请求继续任务",
    )
    thread = threading.Thread(
        target=_run_job,
        args=(job,),
        daemon=True,
        name=f"qlie-{job.job_id}-resume",
    )
    thread.start()
    return job


class QlieWebHandler(BaseHTTPRequestHandler):
    server_version = "NagiQLIE/1"

    def log_message(self, _format, *_args):
        return

    def _local_request(self):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if self.headers.get("Host") not in hosts:
            self._error("invalid local host", HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            self._error("cross-origin request denied", HTTPStatus.FORBIDDEN)
            return False
        return True

    def _headers(
        self, status=HTTPStatus.OK, content_type="application/json; charset=utf-8"
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._headers(status)
        self.wfile.write(data)

    def _error(self, message, status=HTTPStatus.BAD_REQUEST):
        self._json({"error": str(message)}, status)

    def _body(self):
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("invalid request length") from exc
        body_limit = 256 * 1024
        if length < 0 or length > body_limit:
            raise ValueError("request body is too large")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("request must use application/json")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("request body must be an object")
        if payload.get("csrf_token") != self.server.csrf_token:
            raise PermissionError("invalid local request token")
        return payload

    def _static(self, request_path):
        relative = (
            "index.html"
            if request_path in {"", "/"}
            else unquote(request_path.lstrip("/"))
        )
        if relative not in {"index.html", "styles.css", "app.js", "translation-workflow.js", "nagi-avatar.png", "nagi-avatar.svg", "release.js"}:
            self._error("not found", HTTPStatus.NOT_FOUND)
            return
        path = _asset_root() / relative
        if not path.is_file():
            self._error("frontend asset is missing", HTTPStatus.NOT_FOUND)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }
        self._headers(HTTPStatus.OK, content_types[path.suffix])
        self.wfile.write(path.read_bytes())

    def do_GET(self):
        if not self._local_request():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/api/health":
            self._json(
                {
                    "app_id": APP_ID,
                    "version": __version__,
                    "installation_id": installation_id(),
                    "browser_active": self.server.browser_active(),
                }
            )
            return
        if route == "/api/browser-presence":
            self.server.mark_browser_present()
            self._json({"ok": True})
            return
        if route == "/api/settings/locale-emulator":
            self._json(_locale_settings().public())
            return
        if route == "/api/settings/models":
            try:
                self._json(_model_settings().public())
            except (OSError, ValueError) as exc:
                self._error(exc)
            return
        if route == "/api/config":
            model = _configured_model()
            latest = sorted(
                JOBS.list(), key=lambda item: item["created_at"], reverse=True
            )
            self._json(
                {
                    "csrf_token": self.server.csrf_token,
                    "release": capabilities(),
                    "recovery_warning": getattr(self.server, "recovery_warning", None),
                    "provider": model["provider"],
                    "model": model["model"],
                    "api_key_configured": model["api_key_configured"],
                    "output_root": str(_artifact_root()),
                    "latest_job": latest[0] if latest else None,
                    "latest_workflow": self.server.translation_workflows.latest(),
                    "translation_rag": model_status(),
                    "locale_emulator": _locale_settings().public(),
                }
            )
            return
        if route == "/api/agent/workspaces":
            self._json(
                {
                    "workspaces": self.server.agent_service.list_workspaces(),
                    "ungrouped": self.server.agent_service.list_ungrouped_sessions(),
                }
            )
            return
        if route == "/api/agent/sessions":
            workspace_path = (query.get("workspace") or [""])[0]
            self._json(
                {
                    "sessions": self.server.agent_service.list_sessions(
                        workspace_path
                    )
                }
            )
            return
        if route == "/api/agent/jobs":
            job = self.server.agent_service.latest_job()
            self._json({"latest_job": job.public_dict() if job else None})
            return
        if route.startswith("/api/agent/sessions/"):
            workspace_path = (query.get("workspace") or [""])[0]
            session_id = route.rsplit("/", 1)[-1]
            self._json(
                self.server.agent_service.get_session(workspace_path, session_id)
            )
            return
        if route.startswith("/api/agent/jobs/"):
            job = self.server.agent_service.get_job(route.rsplit("/", 1)[-1])
            if not job:
                self._error("agent job not found", HTTPStatus.NOT_FOUND)
                return
            self._json(job.public_dict())
            return
        if route.startswith("/api/translation-workflows/"):
            try:
                self._json(self.server.translation_workflows.get(route.split("/")[-1]).public_dict())
            except ValueError as exc:
                self._error(exc, HTTPStatus.NOT_FOUND)
            return
        if route == "/api/jobs":
            self._json({"jobs": JOBS.list()})
            return
        if route.startswith("/api/jobs/"):
            job = JOBS.get(route.rsplit("/", 1)[-1])
            if not job:
                self._error("job not found", HTTPStatus.NOT_FOUND)
                return
            self._json(job.public_dict())
            return
        if route.startswith("/api/"):
            self._error("not found", HTTPStatus.NOT_FOUND)
            return
        self._static(route)

    def do_POST(self):
        if not self._local_request():
            return
        route = urlparse(self.path).path
        try:
            payload = self._body()
            if route == "/api/settings/onboarding":
                self._json(acknowledge(payload))
                return
            if route == "/api/shutdown":
                agent_jobs = self.server.agent_service.jobs.values()
                if (self.server.agent_service.busy_sessions
                        or any(item.active for item in self.server.translation_workflows.items.values())
                        or any(item["status"] not in TERMINAL_JOB_STATUSES for item in JOBS.list())
                        or any(item.status in {"queued", "running", "awaiting_approval"} for item in agent_jobs)):
                    raise ValueError("仍有任务运行，请先停止任务并等待保存完成，再退出服务。")
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route.startswith("/api/settings/models/"):
                settings = _model_settings()
                revision = payload.get("expected_revision")
                action = route.rsplit("/", 1)[-1]
                if action == "save":
                    result = settings.save(payload.get("provider"), revision, payload.get("activate", False))
                elif action == "activate":
                    result = settings.activate(payload.get("id"), payload.get("model"), revision)
                elif action == "remove":
                    result = settings.remove(payload.get("id"), revision)
                elif action == "test":
                    config = settings.test_config(payload.get("provider"), revision)
                    client = client_from_config(config, timeout=20)
                    reply = client.complete("Reply with OK only.", max_new_tokens=64)
                    if not reply.strip():
                        raise ValueError("接口未返回文本；请检查模型 ID、协议或推理模式")
                    result = {"ok": True, "message": "连接成功，模型已返回文本。配置尚未保存。"}
                else:
                    raise ValueError("不支持的模型设置操作")
                self._json(result)
                return
            if route == "/api/agent/workspaces":
                self._json(
                    self.server.agent_service.add_workspace(payload.get("path", "")),
                    HTTPStatus.CREATED,
                )
                return
            if route == "/api/agent/workspaces/rename":
                self._json(
                    self.server.agent_service.rename_workspace(
                        payload.get("path", ""), payload.get("name", "")
                    )
                )
                return
            if route == "/api/agent/workspaces/remove":
                self._json(
                    self.server.agent_service.remove_workspace(
                        payload.get("path", "")
                    )
                )
                return
            if route == "/api/agent/select-workspace":
                selected = _choose_directory("workspace")
                workspace = (
                    self.server.agent_service.add_workspace(selected)
                    if selected
                    else None
                )
                self._json({"workspace": workspace})
                return
            if route == "/api/agent/sessions":
                session = self.server.agent_service.create_session(
                    payload.get("workspace", "")
                )
                self._json(session, HTTPStatus.CREATED)
                return
            if route.startswith("/api/agent/sessions/") and route.endswith("/rename"):
                session_id = route.split("/")[4]
                self._json(
                    self.server.agent_service.rename_session(
                        payload.get("workspace", ""), session_id, payload.get("title", "")
                    )
                )
                return
            if route.startswith("/api/agent/sessions/") and route.endswith("/command"):
                parts = route.split("/")
                if len(parts) != 6:
                    raise ValueError("invalid command route")
                self._json(self.server.agent_service.run_command(
                    payload.get("workspace", ""), parts[4], payload.get("command", ""),
                    payload.get("arguments", ""), payload.get("max_rounds"), payload.get("approval_policy", "ask"),
                ))
                return
            if route.startswith("/api/agent/sessions/") and route.endswith("/fork"):
                session_id = route.split("/")[4]
                self._json(
                    self.server.agent_service.fork_session(
                        payload.get("workspace", ""), session_id
                    ),
                    HTTPStatus.CREATED,
                )
                return
            if route.startswith("/api/agent/sessions/") and route.endswith("/archive"):
                session_id = route.split("/")[4]
                self._json(
                    self.server.agent_service.archive_session(
                        payload.get("workspace", ""), session_id
                    )
                )
                return
            if route == "/api/agent/jobs":
                job = self.server.agent_service.start_job(
                    workspace_path=payload.get("workspace", ""),
                    session_id=payload.get("session_id"),
                    message=payload.get("message", ""),
                    approval_policy=payload.get("approval_policy", "ask"),
                )
                self._json(job.public_dict(), HTTPStatus.ACCEPTED)
                return
            if route.startswith("/api/agent/jobs/") and route.endswith(
                "/approval"
            ):
                job_id = route.split("/")[4]
                job = self.server.agent_service.get_job(job_id)
                if not job:
                    self._error("agent job not found", HTTPStatus.NOT_FOUND)
                    return
                job.resolve_approval(
                    payload.get("approval_id"), payload.get("approved")
                )
                self._json(job.public_dict())
                return
            if route.startswith("/api/agent/jobs/") and route.endswith("/cancel"):
                job_id = route.split("/")[4]
                job = self.server.agent_service.get_job(job_id)
                if not job:
                    self._error("agent job not found", HTTPStatus.NOT_FOUND)
                    return
                job.cancel()
                self._json(job.public_dict())
                return
            if route == "/api/select-directory":
                self._json({"path": _choose_directory(payload.get("purpose", "game"))})
                return
            if route == "/api/settings/locale-emulator":
                if any(item.active for item in self.server.translation_workflows.items.values()):
                    raise ValueError("翻译流程正在执行，请结束当前步骤后再修改 Locale Emulator。")
                self._json(_locale_settings().save(payload.get("directory", "")))
                return
            if route == "/api/translation-workflows":
                workflow = self.server.translation_workflows.create(
                    payload.get("game_dir", ""), payload.get("storage_dir", ""),
                    reuse_trial=payload.get("reuse_trial") is True)
                self._json(workflow.public_dict(), HTTPStatus.ACCEPTED)
                return
            if route.startswith("/api/translation-workflows/"):
                parts = route.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("无效的翻译流程操作。")
                identifier, action = parts[2:]
                if action == "cancel":
                    workflow = self.server.translation_workflows.cancel(identifier)
                else:
                    workflow = self.server.translation_workflows.start(
                        identifier, action, mode=payload.get("mode", "pilot"),
                        confirmed=payload.get("confirmed", False))
                self._json(workflow.public_dict(), HTTPStatus.ACCEPTED)
                return
            if route == "/api/preflight":
                self._json(assess_game_directory(payload.get("game_dir", "")))
                return
            if route == "/api/jobs":
                if payload.get("confirmed") is not True:
                    raise ValueError("translation cost confirmation is required")
                if "context" in payload:
                    raise ValueError("reference JSON import was removed; refresh the page")
                job = start_translation_job(
                    payload.get("game_dir", ""), payload.get("mode", "pilot"),
                )
                self._json(job.public_dict(), HTTPStatus.ACCEPTED)
                return
            if route.startswith("/api/jobs/") and route.endswith("/cancel"):
                job_id = route.split("/")[3]
                job = JOBS.get(job_id)
                if not job:
                    self._error("job not found", HTTPStatus.NOT_FOUND)
                    return
                if job.status not in TERMINAL_JOB_STATUSES:
                    job.cancel_event.set()
                    job.update(message="将在当前批次结束后停止", event="已请求安全停止")
                self._json(job.public_dict())
                return
            if route.startswith("/api/jobs/") and route.endswith("/resume"):
                job_id = route.split("/")[3]
                job = resume_translation_job(job_id)
                self._json(job.public_dict(), HTTPStatus.ACCEPTED)
                return
            self._error("not found", HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._error(exc, HTTPStatus.FORBIDDEN)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            self._error(exc)

    def do_DELETE(self):
        if not self._local_request():
            return
        route = urlparse(self.path).path
        try:
            payload = self._body()
            if route.startswith("/api/agent/sessions/"):
                session_id = route.rsplit("/", 1)[-1]
                self._json(
                    self.server.agent_service.delete_session(
                        payload.get("workspace", ""), session_id
                    )
                )
                return
            self._error("not found", HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._error(exc, HTTPStatus.FORBIDDEN)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            self._error(exc)


class QlieWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address):
        super().__init__(address, QlieWebHandler)
        self.csrf_token = secrets.token_urlsafe(24)
        self.browser_seen_at = 0.0
        self.recovery_warning = None
        self.translation_workflows = TranslationWorkflows(sys.modules[__name__], deployer=deploy_workflow)
        self.agent_service = AgentWebService(
            state_root() / "web",
            data_root(),
            _agent_model_client,
        )

    def mark_browser_present(self):
        self.browser_seen_at = time.monotonic()

    def browser_active(self):
        return time.monotonic() - self.browser_seen_at < 4.0


def run_web_app(host=DEFAULT_HOST, port=DEFAULT_PORT, *, open_browser=True):
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("QLIE web app may only bind to the local machine")
    ensure_data_directory()
    load_project_env(data_root(), search_parents=False)
    _artifact_root().mkdir(parents=True, exist_ok=True)
    server = QlieWebServer((host, int(port)))
    recovery_path = state_root() / "web" / "latest-translation-workflow.json"
    server.translation_workflows.state_path = recovery_path
    if recovery_path.is_file():
        try:
            recovered = json.loads(recovery_path.read_text(encoding="utf-8"))
            server.translation_workflows.restore(recovered["receipt"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            server.recovery_warning = f"旧任务路径不可用或记录无法恢复，原结果未删除：{exc}。请按升级说明恢复原路径后重启。"
            print(f"未能恢复上次翻译任务（原结果未删除）：{exc}", file=sys.stderr)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Nagi {__version__} Agent 工作台：{url}")
    print("按 Ctrl+C 停止本机服务。")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nagi local Agent and QLIE workbench")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser automatically"
    )
    args = parser.parse_args(argv)
    return run_web_app(args.host, args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
