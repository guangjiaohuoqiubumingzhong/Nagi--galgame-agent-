"""Reproducible, synthetic end-to-end evaluation for the QLIE translation path."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..gameio.qlie.corpus import apply_qlie_corpus_plan, build_qlie_corpus_plan
from ..rag.evaluation import evaluate_retrieval_benchmark, load_retrieval_benchmark
from ..translation import (
    CharacterNameRule,
    TerminologyRule,
    TerminologySnapshot,
    TranslationPreviewSpec,
    TranslationTerm,
    apply_translation_preview,
    build_translation_batch_plan,
    build_translation_patch_preview,
    build_translation_request,
    build_translation_run_spec,
    execute_translation_run,
    load_translation_candidates,
    publish_translation_preview,
    validate_translation_candidates,
)


QLIE_TRANSLATION_BENCHMARK_SCHEMA_VERSION = 1
QLIE_TRANSLATION_ARTIFACT_SCHEMA_VERSION = 1
MAX_BENCHMARK_BYTES = 2 * 1024 * 1024


def _sha256(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(payload):
    return _sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _require_exact_keys(payload, expected, label):
    if not isinstance(payload, dict) or set(payload) != set(expected):
        raise ValueError(f"{label} fields do not match schema version 1")


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _digest(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _is_within(path, parent):
    path_text = os.path.normcase(str(Path(path).resolve()))
    parent_text = os.path.normcase(str(Path(parent).resolve()))
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def _resolve_repo_file(repo_root, value, expected_sha256, label):
    _nonempty(value, f"{label} path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} path must be a safe repository-relative POSIX path")
    path = Path(repo_root).joinpath(*relative.parts).absolute()
    for current in (path, *path.parents):
        if current.exists() and current.is_symlink():
            raise ValueError(f"{label} path is missing or unsafe")
        if current == Path(repo_root):
            break
    if not path.is_file() or not _is_within(path, repo_root):
        raise ValueError(f"{label} path is missing or unsafe")
    if _sha256(path.read_bytes()) != expected_sha256:
        raise ValueError(f"{label} SHA-256 does not match benchmark metadata")
    return path.resolve()


@dataclass(frozen=True)
class QlieTranslationBenchmark:
    dataset_id: str
    dataset_sha256: str
    source_path: str
    fixture: dict
    expected_segment_ids: tuple[str, ...]
    expected_translatable_count: int
    translation: dict
    terminology_snapshot: TerminologySnapshot
    prompt_terms: tuple[TranslationTerm, ...]
    translations_by_source_hash: dict
    retrieval_path: str
    retrieval_sha256: str
    retrieval_repeats: int


@dataclass(frozen=True)
class QlieTranslationEvaluationReport:
    artifact_id: str
    status: str
    dataset: dict
    config: dict
    metrics: dict
    stages: tuple[dict, ...]
    failures: tuple[str, ...]
    environment: dict

    @property
    def passed(self):
        return self.status == "passed"

    def to_dict(self):
        return {
            "schema_version": QLIE_TRANSLATION_ARTIFACT_SCHEMA_VERSION,
            "artifact_id": self.artifact_id,
            "status": self.status,
            "dataset": dict(self.dataset),
            "config": dict(self.config),
            "metrics": dict(self.metrics),
            "stages": [dict(item) for item in self.stages],
            "failures": list(self.failures),
            "environment": dict(self.environment),
            "side_effects": {
                "network_called": False,
                "real_model_called": False,
                "game_modified": False,
                "pack_written": False,
                "translated_sidecar_published": False,
            },
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _load_json_object(path):
    path = Path(path).expanduser().absolute()
    for current in (path, *path.parents):
        if current.exists() and current.is_symlink():
            raise ValueError("translation benchmark must not traverse symbolic links")
    if not path.is_file():
        raise ValueError("translation benchmark must be a regular file")
    path = path.resolve(strict=True)
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_BENCHMARK_BYTES:
        raise ValueError("translation benchmark size is outside the accepted range")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("translation benchmark contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("translation benchmark is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("translation benchmark must contain one JSON object")
    return path, raw, payload


def load_qlie_translation_benchmark(path):
    path, raw, payload = _load_json_object(path)
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "dataset_id",
            "description",
            "fixture",
            "expected",
            "translation",
            "terminology",
            "retrieval",
        },
        "translation benchmark",
    )
    if payload["schema_version"] != QLIE_TRANSLATION_BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported translation benchmark schema version")
    dataset_id = _nonempty(payload["dataset_id"], "dataset_id")
    _nonempty(payload["description"], "description")
    repo_root = path.parent.parent.resolve()

    fixture = payload["fixture"]
    _require_exact_keys(
        fixture,
        {"path", "sha256", "archive_name", "entry_index", "internal_path", "output_path"},
        "fixture",
    )
    _digest(fixture["sha256"], "fixture sha256")
    fixture_path = _resolve_repo_file(repo_root, fixture["path"], fixture["sha256"], "fixture")
    for field in ("archive_name", "internal_path", "output_path"):
        _nonempty(fixture[field], f"fixture {field}")
    if (
        isinstance(fixture["entry_index"], bool)
        or not isinstance(fixture["entry_index"], int)
        or fixture["entry_index"] < 0
    ):
        raise ValueError("fixture entry_index must be a non-negative integer")

    expected = payload["expected"]
    _require_exact_keys(expected, {"segment_ids", "translatable_count"}, "expected")
    segment_ids = expected["segment_ids"]
    if (
        not isinstance(segment_ids, list)
        or not segment_ids
        or any(not isinstance(value, str) or not value.startswith("seg_v1_") for value in segment_ids)
        or len(set(segment_ids)) != len(segment_ids)
    ):
        raise ValueError("expected segment_ids must be unique Segment v1 IDs")
    translatable_count = expected["translatable_count"]
    if (
        isinstance(translatable_count, bool)
        or not isinstance(translatable_count, int)
        or translatable_count < 2
    ):
        raise ValueError("expected translatable_count must be at least two for recovery evaluation")

    translation = payload["translation"]
    _require_exact_keys(
        translation,
        {
            "model_id",
            "prompt_version",
            "terminology_version",
            "target_language",
            "batch_size",
            "translations",
        },
        "translation",
    )
    for field in ("model_id", "prompt_version", "terminology_version", "target_language"):
        _nonempty(translation[field], f"translation {field}")
    if translation["batch_size"] != 1:
        raise ValueError("Phase 6A recovery benchmark requires batch_size 1")
    translations = translation["translations"]
    if not isinstance(translations, list) or len(translations) != translatable_count:
        raise ValueError("translations must cover every expected translatable segment")
    translations_by_hash = {}
    for index, item in enumerate(translations):
        _require_exact_keys(item, {"source_text_sha256", "translated_text"}, f"translation {index}")
        source_hash = _digest(item["source_text_sha256"], "translation source_text_sha256")
        translated_text = _nonempty(item["translated_text"], "translated_text")
        if source_hash in translations_by_hash:
            raise ValueError("translation source hashes must be unique")
        translations_by_hash[source_hash] = translated_text

    terminology = payload["terminology"]
    _require_exact_keys(terminology, {"terms", "characters"}, "terminology")
    term_rules = []
    prompt_terms = []
    for index, item in enumerate(terminology["terms"]):
        _require_exact_keys(item, {"rule_id", "source", "target", "severity"}, f"term {index}")
        rule = TerminologyRule(**item)
        term_rules.append(rule)
        prompt_terms.append(TranslationTerm(rule.source, rule.target, "benchmark rule"))
    character_rules = []
    for index, item in enumerate(terminology["characters"]):
        _require_exact_keys(
            item,
            {"rule_id", "speaker", "canonical", "source_names", "severity"},
            f"character {index}",
        )
        values = dict(item)
        if not isinstance(values["source_names"], list):
            raise ValueError("character source_names must be a list")
        values["source_names"] = tuple(values["source_names"])
        character_rules.append(CharacterNameRule(**values))
    snapshot = TerminologySnapshot(
        version=translation["terminology_version"],
        terms=tuple(term_rules),
        characters=tuple(character_rules),
    )

    retrieval = payload["retrieval"]
    _require_exact_keys(retrieval, {"path", "sha256", "repeats"}, "retrieval")
    _digest(retrieval["sha256"], "retrieval sha256")
    retrieval_path = _resolve_repo_file(
        repo_root, retrieval["path"], retrieval["sha256"], "retrieval benchmark"
    )
    if (
        isinstance(retrieval["repeats"], bool)
        or not isinstance(retrieval["repeats"], int)
        or not 1 <= retrieval["repeats"] <= 1000
    ):
        raise ValueError("retrieval repeats must be between 1 and 1000")

    return QlieTranslationBenchmark(
        dataset_id=dataset_id,
        dataset_sha256=_sha256(raw),
        source_path=str(path),
        fixture={**fixture, "resolved_path": str(fixture_path)},
        expected_segment_ids=tuple(segment_ids),
        expected_translatable_count=translatable_count,
        translation=dict(translation),
        terminology_snapshot=snapshot,
        prompt_terms=tuple(prompt_terms),
        translations_by_source_hash=translations_by_hash,
        retrieval_path=str(retrieval_path),
        retrieval_sha256=retrieval["sha256"],
        retrieval_repeats=retrieval["repeats"],
    )


class _ScriptedTranslationModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **_kwargs):
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("deterministic recovery interruption")
        self.last_completion_metadata = {
            "input_tokens": 0,
            "output_tokens": 0,
            "finish_reason": "synthetic",
        }
        return self.responses.pop(0)


def _response_for_request(request, translations_by_hash):
    rows = []
    for item in request.units:
        translated = translations_by_hash.get(item.unit.source_text_sha256)
        if translated is None:
            raise ValueError("benchmark translations do not cover the planned units")
        rows.append(
            {
                "unit_id": item.unit.unit_id,
                "segment_id": item.unit.segment_id,
                "translated_text": translated,
            }
        )
    return json.dumps(
        {"schema_version": 1, "request_id": request.request_id, "translations": rows},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _write_synthetic_export(benchmark, export_dir):
    fixture = benchmark.fixture
    data = Path(fixture["resolved_path"]).read_bytes()
    output = Path(export_dir).joinpath(*PurePosixPath(fixture["output_path"]).parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    digest = _sha256(data)
    item = {
        "archive_name": fixture["archive_name"],
        "archive_path": "synthetic/data6.pack",
        "archive_rank": 0,
        "compression_flag": 1,
        "conflict_group": None,
        "decoded_sha256": digest,
        "decoded_size": len(data),
        "entry_hash": fixture["entry_index"],
        "entry_hash_hex": f"{fixture['entry_index']:08x}",
        "entry_index": fixture["entry_index"],
        "internal_path": fixture["internal_path"],
        "obfuscation_flag": 2,
        "original_size": len(data),
        "output_path": fixture["output_path"],
        "reason": "synthetic Phase 6A fixture",
        "shadowed_by": None,
        "status": "exported",
        "stored_sha256": digest,
        "stored_size": len(data),
    }
    manifest = {"schema_version": 1, "engine": "qlie", "extensions": [".s"], "items": [item]}
    with (Path(export_dir) / "manifest.json").open(
        "x", encoding="utf-8", newline="\n"
    ) as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _terminology_consistency(units, candidates, snapshot):
    candidate_by_unit = {item.unit_id: item for item in candidates}
    applicable = matched = 0
    for unit in units:
        candidate = candidate_by_unit[unit.unit_id]
        for rule in snapshot.terms:
            count = unit.source_text.count(rule.source)
            applicable += count
            if rule.target is not None:
                matched += min(count, candidate.translated_text.count(rule.target))
        for rule in snapshot.characters:
            count = sum(unit.source_text.count(value) for value in rule.source_names)
            applicable += count
            matched += min(count, candidate.translated_text.count(rule.canonical))
    score = 1.0 if applicable == 0 else matched / applicable
    return applicable, matched, round(score, 6)


def _stage(name, started, status, **details):
    return {
        "name": name,
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 6),
        "details": details,
    }


def evaluate_qlie_translation_benchmark(benchmark, *, workspace_root=None, retrieval_repeats=None):
    if not isinstance(benchmark, QlieTranslationBenchmark):
        raise TypeError("benchmark must be a QlieTranslationBenchmark")
    repeats = benchmark.retrieval_repeats if retrieval_repeats is None else retrieval_repeats
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 1000:
        raise ValueError("retrieval_repeats must be between 1 and 1000")

    owns_workspace = workspace_root is None
    workspace = (
        Path(tempfile.mkdtemp(prefix="nagi-qlie-translation-benchmark-"))
        if owns_workspace
        else Path(workspace_root).expanduser().absolute()
    )
    if not owns_workspace:
        if workspace.parent == workspace:
            raise ValueError("benchmark workspace_root cannot be a filesystem root")
        for current in (workspace, *workspace.parents):
            if current.exists() and current.is_symlink():
                raise ValueError("benchmark workspace_root must not traverse symbolic links")
        if workspace.exists():
            raise ValueError("benchmark workspace_root must not already exist")
        workspace.mkdir(parents=True)

    stages = []
    failures = []
    try:
        export_dir = workspace / "export"
        corpus_dir = workspace / "corpus"

        started = time.perf_counter()
        export_dir.mkdir()
        _write_synthetic_export(benchmark, export_dir)
        corpus_plan = build_qlie_corpus_plan(export_dir, corpus_dir)
        corpus_result = apply_qlie_corpus_plan(corpus_plan)
        corpus_ok = corpus_plan.status == "ready" and corpus_result.status == "published"
        stages.append(
            _stage(
                "extract_and_corpus",
                started,
                "passed" if corpus_ok else "failed",
                segment_count=len(corpus_plan.segments),
                translatable_count=sum(item.translatable for item in corpus_plan.segments),
                unknown_count=sum(item.kind == "unknown" for item in corpus_plan.segments),
            )
        )
        if not corpus_ok:
            raise RuntimeError("synthetic corpus publication failed")

        actual_segment_ids = {item.segment_id for item in corpus_plan.segments}
        expected_segment_ids = set(benchmark.expected_segment_ids)
        recalled = len(expected_segment_ids & actual_segment_ids)
        text_recall = recalled / len(expected_segment_ids)
        if text_recall != 1.0 or actual_segment_ids != expected_segment_ids:
            failures.append("text_recall_mismatch")

        started = time.perf_counter()
        retrieval_benchmark = load_retrieval_benchmark(benchmark.retrieval_path)
        retrieval_report = evaluate_retrieval_benchmark(retrieval_benchmark, repeats=repeats)
        stages.append(
            _stage(
                "retrieval",
                started,
                "passed" if retrieval_report.passed else "failed",
                task_count=len(retrieval_report.task_rows),
                recall_at_k=round(retrieval_report.recall_at_k, 6),
                mrr=round(retrieval_report.mrr, 6),
                search_p50_ms=retrieval_report.latency_ms["p50"],
                search_p95_ms=retrieval_report.latency_ms["p95"],
            )
        )
        if not retrieval_report.passed:
            failures.append("retrieval_recall_below_target")

        started = time.perf_counter()
        plan = build_translation_batch_plan(
            corpus_dir,
            model_id=benchmark.translation["model_id"],
            prompt_version=benchmark.translation["prompt_version"],
            terminology_version=benchmark.translation["terminology_version"],
            rag_index_id=retrieval_report.index_id,
            batch_size=benchmark.translation["batch_size"],
        )
        if plan.status != "ready" or plan.selected_unit_count != benchmark.expected_translatable_count:
            raise RuntimeError("synthetic translation plan does not match benchmark expectations")
        requests = tuple(
            build_translation_request(
                batch,
                model_id=plan.config.model_id,
                prompt_version=plan.config.prompt_version,
                terminology_version=plan.config.terminology_version,
                rag_index_id=plan.config.rag_index_id,
                target_language=benchmark.translation["target_language"],
                terminology=benchmark.prompt_terms,
            )
            for batch in plan.batches
        )
        spec = build_translation_run_spec(plan, requests)
        responses = [
            _response_for_request(request, benchmark.translations_by_source_hash)
            for request in requests
        ]
        run_dir = workspace / "translation-run"
        interrupted_model = _ScriptedTranslationModel(responses[:-1])
        first_run = execute_translation_run(spec, run_dir, interrupted_model)
        resumed_model = _ScriptedTranslationModel(responses[-1:])
        resumed_run = execute_translation_run(spec, run_dir, resumed_model, resume=True)
        candidates = load_translation_candidates(spec, run_dir)
        cached_before_resume = sum(len(request.units) for request in requests[:-1])
        recovery_success = (
            first_run.status == "paused"
            and resumed_run.status == "completed"
            and resumed_run.model_call_count == 1
            and resumed_run.cache_hit_unit_count == cached_before_resume
            and len(candidates) == plan.selected_unit_count
        )
        stages.append(
            _stage(
                "translation_recovery",
                started,
                "passed" if recovery_success else "failed",
                batch_count=len(requests),
                candidate_count=len(candidates),
                initial_model_calls=first_run.model_call_count,
                resume_model_calls=resumed_run.model_call_count,
                cache_hit_units=resumed_run.cache_hit_unit_count,
            )
        )
        if not recovery_success:
            failures.append("recovery_contract_failed")

        units = tuple(unit for batch in plan.batches for unit in batch.units)
        started = time.perf_counter()
        validation = validate_translation_candidates(
            units,
            candidates,
            terminology_snapshot=benchmark.terminology_snapshot,
        )
        expected_tokens = validation.expected_token_count
        structure_retention = (
            1.0 if expected_tokens == 0 else validation.preserved_token_count / expected_tokens
        )
        applicable_terms, matched_terms, terminology_consistency = _terminology_consistency(
            units, candidates, benchmark.terminology_snapshot
        )
        quality_ok = (
            validation.patch_eligible
            and structure_retention == 1.0
            and terminology_consistency == 1.0
        )
        stages.append(
            _stage(
                "quality_gates",
                started,
                "passed" if quality_ok else "failed",
                unit_count=len(validation.reports),
                issue_count=validation.issue_count,
                structure_retention=round(structure_retention, 6),
                terminology_consistency=terminology_consistency,
            )
        )
        if not validation.patch_eligible:
            failures.append("quality_gate_failed")
        if structure_retention != 1.0:
            failures.append("structure_retention_below_target")
        if terminology_consistency != 1.0:
            failures.append("terminology_consistency_below_target")

        dry_run_ok = False
        changed_units = 0
        changed_files = 0
        started = time.perf_counter()
        if validation.patch_eligible:
            preview_spec = TranslationPreviewSpec(
                run_id=spec.run_id,
                plan_id=plan.plan_id,
                corpus_dir=str(corpus_dir),
                segments_sha256=plan.segments_sha256,
                parse_report_sha256=plan.parse_report_sha256,
            )
            preview = build_translation_patch_preview(
                units,
                candidates,
                preview_spec,
                current_segments_sha256=plan.segments_sha256,
                current_parse_report_sha256=plan.parse_report_sha256,
                current_source_hashes={unit.unit_id: unit.source.source_sha256 for unit in units},
                terminology_snapshot=benchmark.terminology_snapshot,
                expected_candidate_hashes={
                    candidate.unit_id: candidate.translated_text_sha256 for candidate in candidates
                },
            )
            preview_dir = workspace / "preview"
            publish_translation_preview(preview, preview_dir, include_text=True)
            apply_result = apply_translation_preview(
                preview_dir,
                export_dir,
                workspace / "translated-sidecar",
            )
            dry_run_ok = (
                apply_result.status == "dry_run"
                and not (workspace / "translated-sidecar").exists()
            )
            changed_units = apply_result.changed_unit_count
            changed_files = apply_result.changed_file_count
        stages.append(
            _stage(
                "patch_dry_run",
                started,
                "passed" if dry_run_ok else "failed",
                changed_unit_count=changed_units,
                changed_file_count=changed_files,
                sidecar_published=False,
            )
        )
        if not dry_run_ok:
            failures.append("patch_dry_run_failed")

        failures = tuple(dict.fromkeys(failures))
        metrics = {
            "text_recall": round(text_recall, 6),
            "retrieval_recall_at_k": round(retrieval_report.recall_at_k, 6),
            "retrieval_mrr": round(retrieval_report.mrr, 6),
            "structure_retention": round(structure_retention, 6),
            "terminology_consistency": terminology_consistency,
            "recovery_success_rate": 1.0 if recovery_success else 0.0,
            "expected_token_count": expected_tokens,
            "preserved_token_count": validation.preserved_token_count,
            "applicable_terminology_count": applicable_terms,
            "matched_terminology_count": matched_terms,
            "candidate_count": len(candidates),
            "dry_run_changed_unit_count": changed_units,
            "dry_run_changed_file_count": changed_files,
            "estimated_cost_usd": 0.0,
        }
        config = {
            "model": benchmark.translation["model_id"],
            "prompt_version": benchmark.translation["prompt_version"],
            "terminology_version": benchmark.translation["terminology_version"],
            "target_language": benchmark.translation["target_language"],
            "batch_size": benchmark.translation["batch_size"],
            "retrieval_repeats": repeats,
            "provider": "deterministic-scripted",
        }
        status = "passed" if not failures else "failed"
        stable_identity = {
            "schema_version": QLIE_TRANSLATION_ARTIFACT_SCHEMA_VERSION,
            "dataset_sha256": benchmark.dataset_sha256,
            "fixture_sha256": benchmark.fixture["sha256"],
            "retrieval_sha256": benchmark.retrieval_sha256,
            "segments_sha256": corpus_result.segments_sha256,
            "config": config,
            "metrics": metrics,
            "stage_statuses": [(item["name"], item["status"]) for item in stages],
            "failures": list(failures),
        }
        return QlieTranslationEvaluationReport(
            artifact_id="qlie_translation_eval_v1_" + _canonical_sha256(stable_identity),
            status=status,
            dataset={
                "dataset_id": benchmark.dataset_id,
                "dataset_sha256": benchmark.dataset_sha256,
                "fixture_sha256": benchmark.fixture["sha256"],
                "retrieval_dataset_sha256": benchmark.retrieval_sha256,
                "segments_sha256": corpus_result.segments_sha256,
                "provenance": "original synthetic fixtures; no commercial game text",
            },
            config=config,
            metrics=metrics,
            stages=tuple(stages),
            failures=failures,
            environment={
                "python": platform.python_version(),
                "platform": platform.system().lower(),
                "machine": platform.machine().lower(),
            },
        )
    finally:
        if owns_workspace:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)


def write_qlie_translation_artifact(report, output_path):
    if not isinstance(report, QlieTranslationEvaluationReport):
        raise TypeError("report must be a QlieTranslationEvaluationReport")
    output = Path(output_path).expanduser().absolute()
    for current in (output, *output.parents):
        if current.exists() and current.is_symlink():
            raise ValueError("artifact output must not traverse symbolic links")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(output.parent),
        prefix=output.name + ".",
        suffix=".tmp",
    ) as stream:
        stream.write(report.to_json())
        temporary = Path(stream.name)
    temporary.replace(output)
    return output.resolve()


def render_qlie_translation_evaluation_text(report):
    if not isinstance(report, QlieTranslationEvaluationReport):
        raise TypeError("report must be a QlieTranslationEvaluationReport")
    metrics = report.metrics
    return "\n".join(
        (
            f"QLIE translation benchmark: {report.status}",
            f"artifact_id: {report.artifact_id}",
            (
                "text_recall={text_recall:.6f}; retrieval_recall_at_k={retrieval_recall_at_k:.6f}; "
                "retrieval_mrr={retrieval_mrr:.6f}"
            ).format(**metrics),
            (
                "structure_retention={structure_retention:.6f}; "
                "terminology_consistency={terminology_consistency:.6f}; "
                "recovery_success_rate={recovery_success_rate:.6f}"
            ).format(**metrics),
            f"failures: {', '.join(report.failures) if report.failures else 'none'}",
            "side_effects: network_called=false; real_model_called=false; game_modified=false; pack_written=false",
        )
    )


__all__ = [
    "QLIE_TRANSLATION_ARTIFACT_SCHEMA_VERSION",
    "QLIE_TRANSLATION_BENCHMARK_SCHEMA_VERSION",
    "QlieTranslationBenchmark",
    "QlieTranslationEvaluationReport",
    "evaluate_qlie_translation_benchmark",
    "load_qlie_translation_benchmark",
    "render_qlie_translation_evaluation_text",
    "write_qlie_translation_artifact",
]
