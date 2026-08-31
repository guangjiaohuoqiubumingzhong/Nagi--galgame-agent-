"""Durable candidate cache and resumable translation-run checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import RAGEvidenceReference, TranslationCandidate, _canonical_sha256
from .planner import TranslationBatchPlan
from .translator import (
    DEFAULT_MAX_NEW_TOKENS,
    TranslationRequest,
    execute_translation_batch,
)


TRANSLATION_RUN_SCHEMA_VERSION = 1
TRANSLATION_RUN_VERSION = 1
TRANSLATION_CHECKPOINT_VERSION = 1
TRANSLATION_CANDIDATE_ARTIFACT_VERSION = 1
TRANSLATION_RUN_ID_PREFIX = "trun_v1_"
RUN_STATUSES = frozenset({"completed", "paused"})
BATCH_RUN_STATUSES = frozenset({"pending", "completed", "failed"})
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
COMMIT_RENAME_ATTEMPTS = 6
COMMIT_RENAME_DELAY_SECONDS = 0.05


class TranslationCacheError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path, parent):
    path_text = os.path.normcase(str(Path(path).resolve()))
    parent_text = os.path.normcase(str(Path(parent).resolve()))
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def _git_root_for(path):
    candidate = Path(path).resolve()
    if not candidate.is_dir():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists():
            return current
    return None


def _validate_output_location(spec, output):
    source_root = Path(spec.corpus_dir).resolve()
    if output.parent == output:
        raise TranslationCacheError(
            "unsafe_output",
            "filesystem root cannot be used as a translation run directory",
        )
    if _is_within(output, source_root) or _is_within(source_root, output):
        raise TranslationCacheError(
            "unsafe_output",
            "translation run directory must not overlap the source corpus",
        )
    git_root = _git_root_for(output)
    if git_root is not None:
        raise TranslationCacheError(
            "unsafe_output",
            f"translation run directory must be outside Git worktree: {git_root}",
        )


def _resolve_output_dir(output_dir):
    candidate = Path(output_dir).expanduser().absolute()
    for current in (candidate, *candidate.parents):
        if current.exists() and current.is_symlink():
            raise TranslationCacheError(
                "unsafe_output",
                "translation run path must not traverse symbolic links",
            )
    return candidate.resolve()


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _commit_staging_directory(staging, output):
    for attempt in range(COMMIT_RENAME_ATTEMPTS):
        try:
            os.rename(staging, output)
            return
        except PermissionError:
            if attempt + 1 == COMMIT_RENAME_ATTEMPTS:
                raise
            time.sleep(COMMIT_RENAME_DELAY_SECONDS * (2**attempt))


def _load_json(path, *, max_bytes, label):
    path = Path(path)
    if path.is_symlink():
        raise TranslationCacheError("unsafe_artifact", f"{label} must not be a symlink")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TranslationCacheError("missing_artifact", f"missing {label}") from exc
    if size <= 0 or size > max_bytes:
        raise TranslationCacheError(
            "invalid_artifact_size",
            f"{label} size is outside the accepted range",
        )

    def reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise TranslationCacheError(
                    "invalid_json",
                    f"{label} contains a duplicate JSON key",
                )
            value[key] = item
        return value

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except TranslationCacheError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TranslationCacheError("invalid_json", f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise TranslationCacheError("invalid_schema", f"{label} must be a JSON object")
    return payload


@dataclass(frozen=True)
class TranslationRunSpec:
    run_id: str
    plan_id: str
    corpus_dir: str
    segments_sha256: str
    parse_report_sha256: str
    requests: tuple[TranslationRequest, ...] = field(repr=False)

    def __post_init__(self):
        if not self.run_id.startswith(TRANSLATION_RUN_ID_PREFIX):
            raise ValueError("run_id has an unsupported version")
        if not self.plan_id.startswith("tp_v1_"):
            raise ValueError("plan_id has an unsupported version")
        if not self.requests:
            raise ValueError("translation run must contain requests")
        if not isinstance(self.corpus_dir, str) or not self.corpus_dir:
            raise ValueError("corpus_dir must be a non-empty string")
        for value, field_name in (
            (self.segments_sha256, "segments_sha256"),
            (self.parse_report_sha256, "parse_report_sha256"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")

    def manifest_dict(self):
        return {
            "schema_version": TRANSLATION_RUN_SCHEMA_VERSION,
            "run_version": TRANSLATION_RUN_VERSION,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "source": {
                "corpus_dir": self.corpus_dir,
                "segments_sha256": self.segments_sha256,
                "parse_report_sha256": self.parse_report_sha256,
            },
            "summary": {
                "batch_count": len(self.requests),
                "unit_count": sum(len(request.units) for request in self.requests),
            },
            "requests": [
                {
                    "ordinal": ordinal,
                    "request_id": request.request_id,
                    "batch_id": request.batch_id,
                    "prompt_sha256": request.prompt_sha256,
                    "unit_count": len(request.units),
                    "cache_keys": [item.cache_key for item in request.units],
                }
                for ordinal, request in enumerate(self.requests, start=1)
            ],
            "side_effects": {
                "game_modified": False,
            },
        }


def build_translation_run_spec(plan, requests):
    if not isinstance(plan, TranslationBatchPlan):
        raise TypeError("plan must be a TranslationBatchPlan")
    if plan.status != "ready" or not plan.plan_id:
        raise ValueError("only ready translation plans can become runs")
    values = tuple(requests)
    if len(values) != len(plan.batches):
        raise ValueError("translation requests must cover every planned batch")
    if any(not isinstance(item, TranslationRequest) for item in values):
        raise TypeError("requests must contain TranslationRequest values")
    for batch, request in zip(plan.batches, values):
        if request.batch_id != batch.batch_id:
            raise ValueError("request batch order does not match the translation plan")
        if [item.unit.unit_id for item in request.units] != [
            unit.unit_id for unit in batch.units
        ]:
            raise ValueError("request units do not match the planned batch")
        if (
            request.model_id != plan.config.model_id
            or request.prompt_version != plan.config.prompt_version
            or request.terminology_version != plan.config.terminology_version
            or request.rag_index_id != plan.config.rag_index_id
        ):
            raise ValueError("request configuration does not match the translation plan")
    identity = {
        "schema_version": TRANSLATION_RUN_SCHEMA_VERSION,
        "run_version": TRANSLATION_RUN_VERSION,
        "plan_id": plan.plan_id,
        "segments_sha256": plan.segments_sha256,
        "parse_report_sha256": plan.parse_report_sha256,
        "request_ids": [item.request_id for item in values],
    }
    run_id = TRANSLATION_RUN_ID_PREFIX + _canonical_sha256(identity)
    return TranslationRunSpec(
        run_id=run_id,
        plan_id=plan.plan_id,
        corpus_dir=plan.corpus_dir,
        segments_sha256=plan.segments_sha256,
        parse_report_sha256=plan.parse_report_sha256,
        requests=values,
    )


def _new_checkpoint(spec, manifest_sha256):
    return {
        "schema_version": TRANSLATION_RUN_SCHEMA_VERSION,
        "checkpoint_version": TRANSLATION_CHECKPOINT_VERSION,
        "run_id": spec.run_id,
        "plan_id": spec.plan_id,
        "manifest_sha256": manifest_sha256,
        "revision": 0,
        "batches": [
            {
                "ordinal": ordinal,
                "batch_id": request.batch_id,
                "request_id": request.request_id,
                "status": "pending",
                "attempts": 0,
                "reason": "",
                "response_sha256": None,
                "candidate_files": [],
            }
            for ordinal, request in enumerate(spec.requests, start=1)
        ],
        "side_effects": {
            "game_modified": False,
        },
    }


def initialize_translation_run(spec, output_dir, *, context_config=None):
    if not isinstance(spec, TranslationRunSpec):
        raise TypeError("spec must be a TranslationRunSpec")
    output = _resolve_output_dir(output_dir)
    _validate_output_location(spec, output)
    if output.exists():
        raise TranslationCacheError(
            "output_exists",
            "translation run directory already exists; use resume to continue it",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".staging-", dir=str(output.parent)))
    try:
        (staging / "candidates").mkdir()
        manifest_path = staging / "manifest.json"
        _write_json_atomic(manifest_path, spec.manifest_dict())
        checkpoint = _new_checkpoint(spec, _sha256_file(manifest_path))
        _write_json_atomic(staging / "checkpoint.json", checkpoint)
        if context_config is not None:
            _write_json_atomic(staging / "context-config.json", context_config)
        _commit_staging_directory(staging, output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return output


def _require_exact_keys(payload, expected, label):
    if not isinstance(payload, dict):
        raise TranslationCacheError("invalid_schema", f"{label} must be an object")
    if set(payload) != set(expected):
        raise TranslationCacheError("invalid_schema", f"{label} has unexpected fields")


def _validate_manifest(spec, output):
    manifest_path = output / "manifest.json"
    manifest = _load_json(manifest_path, max_bytes=MAX_MANIFEST_BYTES, label="manifest")
    if _canonical_sha256(manifest) != _canonical_sha256(spec.manifest_dict()):
        raise TranslationCacheError(
            "manifest_identity_mismatch",
            "translation manifest does not match the current corpus, plan, or requests",
        )
    return manifest_path


def _validate_checkpoint_shape(spec, checkpoint, manifest_sha256):
    _require_exact_keys(
        checkpoint,
        {
            "schema_version",
            "checkpoint_version",
            "run_id",
            "plan_id",
            "manifest_sha256",
            "revision",
            "batches",
            "side_effects",
        },
        "checkpoint",
    )
    if (
        isinstance(checkpoint["schema_version"], bool)
        or isinstance(checkpoint["checkpoint_version"], bool)
        or checkpoint["schema_version"] != TRANSLATION_RUN_SCHEMA_VERSION
        or checkpoint["checkpoint_version"] != TRANSLATION_CHECKPOINT_VERSION
    ):
        raise TranslationCacheError("version_mismatch", "unsupported checkpoint version")
    if checkpoint["run_id"] != spec.run_id or checkpoint["plan_id"] != spec.plan_id:
        raise TranslationCacheError("checkpoint_identity_mismatch", "checkpoint run identity does not match")
    if checkpoint["manifest_sha256"] != manifest_sha256:
        raise TranslationCacheError("manifest_hash_mismatch", "checkpoint manifest hash does not match")
    if (
        isinstance(checkpoint["revision"], bool)
        or not isinstance(checkpoint["revision"], int)
        or checkpoint["revision"] < 0
    ):
        raise TranslationCacheError("invalid_schema", "checkpoint revision is invalid")
    if (
        not isinstance(checkpoint["side_effects"], dict)
        or set(checkpoint["side_effects"]) != {"game_modified"}
        or checkpoint["side_effects"]["game_modified"] is not False
    ):
        raise TranslationCacheError("invalid_schema", "checkpoint side effects are invalid")
    batches = checkpoint["batches"]
    if not isinstance(batches, list) or len(batches) != len(spec.requests):
        raise TranslationCacheError("checkpoint_batch_mismatch", "checkpoint batch count does not match")
    expected_keys = {
        "ordinal",
        "batch_id",
        "request_id",
        "status",
        "attempts",
        "reason",
        "response_sha256",
        "candidate_files",
    }
    for ordinal, (request, batch) in enumerate(zip(spec.requests, batches), start=1):
        if not isinstance(batch, dict):
            raise TranslationCacheError("invalid_schema", "checkpoint batch must be an object")
        _require_exact_keys(batch, expected_keys, "checkpoint batch")
        if (
            isinstance(batch["ordinal"], bool)
            or batch["ordinal"] != ordinal
            or batch["batch_id"] != request.batch_id
            or batch["request_id"] != request.request_id
        ):
            raise TranslationCacheError("checkpoint_batch_mismatch", "checkpoint batch identity does not match")
        if batch["status"] not in BATCH_RUN_STATUSES:
            raise TranslationCacheError("invalid_schema", "checkpoint batch status is invalid")
        if (
            isinstance(batch["attempts"], bool)
            or not isinstance(batch["attempts"], int)
            or batch["attempts"] < 0
        ):
            raise TranslationCacheError("invalid_schema", "checkpoint attempts is invalid")
        if not isinstance(batch["reason"], str) or not isinstance(batch["candidate_files"], list):
            raise TranslationCacheError("invalid_schema", "checkpoint batch fields are invalid")
        if batch["response_sha256"] is not None and (
            not isinstance(batch["response_sha256"], str)
            or len(batch["response_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in batch["response_sha256"])
        ):
            raise TranslationCacheError("invalid_schema", "checkpoint response hash is invalid")
        if batch["status"] == "pending" and (
            batch["attempts"] != 0 or batch["candidate_files"]
        ):
            raise TranslationCacheError("invalid_schema", "pending batch contains completion state")
        if batch["status"] == "failed" and batch["candidate_files"]:
            raise TranslationCacheError("invalid_schema", "failed batch contains candidates")
        if batch["status"] == "completed" and len(batch["candidate_files"]) != len(request.units):
            raise TranslationCacheError("candidate_count_mismatch", "completed batch candidate count does not match")


def _candidate_from_payload(payload):
    _require_exact_keys(
        payload,
        {
            "candidate_id",
            "unit_id",
            "segment_id",
            "cache_key",
            "translated_text",
            "translated_text_sha256",
            "status",
            "model_id",
            "prompt_version",
            "terminology_version",
            "rag_index_id",
            "rag_evidence",
            "warnings",
        },
        "candidate",
    )
    evidence = payload["rag_evidence"]
    warnings = payload["warnings"]
    if not isinstance(evidence, list) or not isinstance(warnings, list):
        raise TranslationCacheError("invalid_schema", "candidate arrays are invalid")
    try:
        values = dict(payload)
        values["rag_evidence"] = tuple(RAGEvidenceReference(**item) for item in evidence)
        values["warnings"] = tuple(warnings)
        return TranslationCandidate(**values)
    except (TypeError, ValueError) as exc:
        raise TranslationCacheError("invalid_candidate", "candidate contract validation failed") from exc


def _validate_candidate_artifact(output, spec, request, request_unit, reference):
    _require_exact_keys(reference, {"cache_key", "path", "sha256"}, "candidate file reference")
    expected_relative = f"candidates/{request_unit.cache_key}.json"
    if reference["cache_key"] != request_unit.cache_key or reference["path"] != expected_relative:
        raise TranslationCacheError("candidate_identity_mismatch", "candidate file reference does not match")
    path = output / Path(expected_relative)
    if not _is_within(path, output):
        raise TranslationCacheError("unsafe_artifact", "candidate path escapes the run directory")
    try:
        actual_sha256 = _sha256_file(path)
    except OSError as exc:
        raise TranslationCacheError("missing_artifact", "candidate artifact is missing") from exc
    if actual_sha256 != reference["sha256"]:
        raise TranslationCacheError("candidate_hash_mismatch", "candidate artifact hash does not match")
    artifact = _load_json(path, max_bytes=MAX_CANDIDATE_BYTES, label="candidate artifact")
    _require_exact_keys(
        artifact,
        {
            "schema_version",
            "artifact_version",
            "run_id",
            "request_id",
            "batch_id",
            "candidate",
        },
        "candidate artifact",
    )
    if (
        isinstance(artifact["schema_version"], bool)
        or isinstance(artifact["artifact_version"], bool)
        or artifact["schema_version"] != TRANSLATION_RUN_SCHEMA_VERSION
        or artifact["artifact_version"] != TRANSLATION_CANDIDATE_ARTIFACT_VERSION
        or artifact["run_id"] != spec.run_id
        or artifact["request_id"] != request.request_id
        or artifact["batch_id"] != request.batch_id
    ):
        raise TranslationCacheError("candidate_identity_mismatch", "candidate artifact identity does not match")
    candidate = _candidate_from_payload(artifact["candidate"])
    if (
        candidate.unit_id != request_unit.unit.unit_id
        or candidate.segment_id != request_unit.unit.segment_id
        or candidate.cache_key != request_unit.cache_key
        or candidate.model_id != request.model_id
        or candidate.prompt_version != request.prompt_version
        or candidate.terminology_version != request.terminology_version
        or candidate.rag_index_id != request.rag_index_id
    ):
        raise TranslationCacheError("candidate_identity_mismatch", "candidate does not match the request unit")
    return candidate


def load_translation_checkpoint(spec, output_dir):
    if not isinstance(spec, TranslationRunSpec):
        raise TypeError("spec must be a TranslationRunSpec")
    output = _resolve_output_dir(output_dir)
    _validate_output_location(spec, output)
    if not output.is_dir() or output.is_symlink():
        raise TranslationCacheError("missing_run", "translation run directory does not exist")
    manifest_path = _validate_manifest(spec, output)
    checkpoint_path = output / "checkpoint.json"
    checkpoint = _load_json(
        checkpoint_path,
        max_bytes=MAX_CHECKPOINT_BYTES,
        label="checkpoint",
    )
    _validate_checkpoint_shape(spec, checkpoint, _sha256_file(manifest_path))
    for request, batch in zip(spec.requests, checkpoint["batches"]):
        if batch["status"] != "completed":
            continue
        for request_unit, reference in zip(request.units, batch["candidate_files"]):
            _validate_candidate_artifact(
                output,
                spec,
                request,
                request_unit,
                reference,
            )
    return checkpoint


def load_translation_candidates(spec, output_dir):
    """Load a complete run's candidates in request order after full validation."""

    if not isinstance(spec, TranslationRunSpec):
        raise TypeError("spec must be a TranslationRunSpec")
    output = _resolve_output_dir(output_dir)
    checkpoint = load_translation_checkpoint(spec, output)
    if any(batch["status"] != "completed" for batch in checkpoint["batches"]):
        raise TranslationCacheError(
            "run_incomplete",
            "translation candidates are unavailable until every batch is completed",
        )
    candidates = []
    for request, batch in zip(spec.requests, checkpoint["batches"]):
        for request_unit, reference in zip(request.units, batch["candidate_files"]):
            candidates.append(
                _validate_candidate_artifact(
                    output,
                    spec,
                    request,
                    request_unit,
                    reference,
                )
            )
    return tuple(candidates)


def _candidate_artifact(spec, request, candidate):
    return {
        "schema_version": TRANSLATION_RUN_SCHEMA_VERSION,
        "artifact_version": TRANSLATION_CANDIDATE_ARTIFACT_VERSION,
        "run_id": spec.run_id,
        "request_id": request.request_id,
        "batch_id": request.batch_id,
        "candidate": candidate.to_dict(include_text=True),
    }


def _write_completed_batch(output, spec, request, result, batch_state, checkpoint):
    references = []
    for request_unit, candidate in zip(request.units, result.candidates):
        if candidate.cache_key != request_unit.cache_key:
            raise TranslationCacheError("candidate_identity_mismatch", "generated candidate cache key does not match")
        relative = f"candidates/{candidate.cache_key}.json"
        path = output / Path(relative)
        _write_json_atomic(path, _candidate_artifact(spec, request, candidate))
        references.append(
            {
                "cache_key": candidate.cache_key,
                "path": relative,
                "sha256": _sha256_file(path),
            }
        )
    batch_state["status"] = "completed"
    batch_state["reason"] = result.reason
    batch_state["response_sha256"] = result.response_sha256
    batch_state["candidate_files"] = references
    checkpoint["revision"] += 1
    _write_json_atomic(output / "checkpoint.json", checkpoint)


def _write_failed_batch(output, result, batch_state, checkpoint):
    batch_state["status"] = "failed"
    batch_state["reason"] = result.reason
    batch_state["response_sha256"] = result.response_sha256
    batch_state["candidate_files"] = []
    checkpoint["revision"] += 1
    _write_json_atomic(output / "checkpoint.json", checkpoint)


def _write_attempt_started(output, batch_state, checkpoint):
    batch_state["status"] = "failed"
    batch_state["reason"] = "attempt_started"
    batch_state["response_sha256"] = None
    batch_state["candidate_files"] = []
    checkpoint["revision"] += 1
    _write_json_atomic(output / "checkpoint.json", checkpoint)


def _validate_model_execution_inputs(model_client, max_new_tokens):
    if not callable(getattr(model_client, "complete", None)):
        raise TypeError("model_client must expose complete(prompt, max_new_tokens)")
    if (
        isinstance(max_new_tokens, bool)
        or not isinstance(max_new_tokens, int)
        or max_new_tokens < 1
    ):
        raise ValueError("max_new_tokens must be a positive integer")


@dataclass(frozen=True)
class TranslationRunResult:
    status: str
    reason: str
    run_id: str
    plan_id: str
    output_dir: str
    revision: int
    completed_batch_count: int
    pending_batch_count: int
    failed_batch_count: int
    completed_unit_count: int
    cache_hit_unit_count: int
    model_call_count: int
    attempt_count: int
    retry_count: int
    output_written: bool
    request_id: str | None = None
    batch_id: str | None = None
    response_sha256: str | None = None
    completion_metadata: dict = field(default_factory=dict, repr=False)
    artifact_references: tuple[dict, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if self.status not in RUN_STATUSES:
            raise ValueError("unsupported translation run result status")

    def to_dict(self):
        return {
            "schema_version": TRANSLATION_RUN_SCHEMA_VERSION,
            "status": self.status,
            "reason": self.reason,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "output_dir": self.output_dir,
            "checkpoint_revision": self.revision,
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "response_sha256": self.response_sha256,
            "completion_metadata": dict(self.completion_metadata),
            "artifact_references": [dict(item) for item in self.artifact_references],
            "summary": {
                "completed_batch_count": self.completed_batch_count,
                "pending_batch_count": self.pending_batch_count,
                "failed_batch_count": self.failed_batch_count,
                "completed_unit_count": self.completed_unit_count,
                "cache_hit_unit_count": self.cache_hit_unit_count,
                "model_call_count": self.model_call_count,
                "attempt_count": self.attempt_count,
                "retry_count": self.retry_count,
            },
            "side_effects": {
                "model_called": self.model_call_count > 0,
                "output_written": self.output_written,
                "game_modified": False,
            },
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def _run_result(
    spec,
    output,
    checkpoint,
    *,
    reason,
    model_calls,
    cache_hit_units,
    output_written,
    execution_result=None,
    batch_state=None,
):
    completed = [item for item in checkpoint["batches"] if item["status"] == "completed"]
    failed = [item for item in checkpoint["batches"] if item["status"] == "failed"]
    pending = [item for item in checkpoint["batches"] if item["status"] == "pending"]
    completed_units = sum(
        len(request.units)
        for request, state in zip(spec.requests, checkpoint["batches"])
        if state["status"] == "completed"
    )
    attempts = sum(item["attempts"] for item in checkpoint["batches"])
    return TranslationRunResult(
        status="completed" if len(completed) == len(spec.requests) else "paused",
        reason=reason,
        run_id=spec.run_id,
        plan_id=spec.plan_id,
        output_dir=str(output),
        revision=checkpoint["revision"],
        completed_batch_count=len(completed),
        pending_batch_count=len(pending),
        failed_batch_count=len(failed),
        completed_unit_count=completed_units,
        cache_hit_unit_count=cache_hit_units,
        model_call_count=model_calls,
        attempt_count=attempts,
        retry_count=sum(max(item["attempts"] - 1, 0) for item in checkpoint["batches"]),
        output_written=output_written,
        request_id=(execution_result.request_id if execution_result is not None else None),
        batch_id=(execution_result.batch_id if execution_result is not None else None),
        response_sha256=(
            execution_result.response_sha256 if execution_result is not None else None
        ),
        completion_metadata=(
            dict(execution_result.completion_metadata)
            if execution_result is not None
            else {}
        ),
        artifact_references=tuple(
            dict(item) for item in (batch_state or {}).get("candidate_files", [])
        ),
    )


def inspect_translation_run(spec, output_dir):
    """Validate a run and return content-free progress without model or file writes."""

    output = _resolve_output_dir(output_dir)
    checkpoint = load_translation_checkpoint(spec, output)
    cache_hit_units = sum(
        len(request.units)
        for request, state in zip(spec.requests, checkpoint["batches"])
        if state["status"] == "completed"
    )
    return _run_result(
        spec,
        output,
        checkpoint,
        reason=(
            "all_batches_completed"
            if all(item["status"] == "completed" for item in checkpoint["batches"])
            else "run_has_pending_batches"
        ),
        model_calls=0,
        cache_hit_units=cache_hit_units,
        output_written=False,
    )


def execute_translation_run_next(
    spec,
    output_dir,
    model_client,
    *,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
):
    """Execute at most one pending or failed batch from an initialized run."""

    if not isinstance(spec, TranslationRunSpec):
        raise TypeError("spec must be a TranslationRunSpec")
    _validate_model_execution_inputs(model_client, max_new_tokens)
    output = _resolve_output_dir(output_dir)
    checkpoint = load_translation_checkpoint(spec, output)
    cache_hit_units = sum(
        len(request.units)
        for request, state in zip(spec.requests, checkpoint["batches"])
        if state["status"] == "completed"
    )
    selected = next(
        (
            (request, state)
            for request, state in zip(spec.requests, checkpoint["batches"])
            if state["status"] != "completed"
        ),
        None,
    )
    if selected is None:
        return _run_result(
            spec,
            output,
            checkpoint,
            reason="all_batches_completed",
            model_calls=0,
            cache_hit_units=cache_hit_units,
            output_written=False,
        )

    request, batch_state = selected
    batch_state["attempts"] += 1
    _write_attempt_started(output, batch_state, checkpoint)
    result = execute_translation_batch(
        request,
        model_client,
        max_new_tokens=max_new_tokens,
    )
    if result.status != "completed":
        _write_failed_batch(output, result, batch_state, checkpoint)
        return _run_result(
            spec,
            output,
            checkpoint,
            reason=result.reason,
            model_calls=1,
            cache_hit_units=cache_hit_units,
            output_written=True,
            execution_result=result,
            batch_state=batch_state,
        )
    _write_completed_batch(output, spec, request, result, batch_state, checkpoint)
    all_completed = all(item["status"] == "completed" for item in checkpoint["batches"])
    return _run_result(
        spec,
        output,
        checkpoint,
        reason="all_batches_completed" if all_completed else "batch_completed",
        model_calls=1,
        cache_hit_units=cache_hit_units,
        output_written=True,
        execution_result=result,
        batch_state=batch_state,
    )


def execute_translation_run(
    spec,
    output_dir,
    model_client,
    *,
    resume=False,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
):
    """Execute requests in order, checkpoint each batch, and stop on first failure."""

    if not isinstance(spec, TranslationRunSpec):
        raise TypeError("spec must be a TranslationRunSpec")
    _validate_model_execution_inputs(model_client, max_new_tokens)
    output = _resolve_output_dir(output_dir)
    if resume:
        checkpoint = load_translation_checkpoint(spec, output)
        output_written = False
    else:
        initialize_translation_run(spec, output)
        checkpoint = load_translation_checkpoint(spec, output)
        output_written = True

    cache_hit_units = sum(
        len(request.units)
        for request, state in zip(spec.requests, checkpoint["batches"])
        if state["status"] == "completed"
    )
    model_calls = 0
    last_execution_result = None
    last_batch_state = None
    for request, batch_state in zip(spec.requests, checkpoint["batches"]):
        if batch_state["status"] == "completed":
            continue
        batch_state["attempts"] += 1
        _write_attempt_started(output, batch_state, checkpoint)
        output_written = True
        model_calls += 1
        result = execute_translation_batch(
            request,
            model_client,
            max_new_tokens=max_new_tokens,
        )
        last_execution_result = result
        last_batch_state = batch_state
        if result.status != "completed":
            _write_failed_batch(output, result, batch_state, checkpoint)
            return _run_result(
                spec,
                output,
                checkpoint,
                reason=result.reason,
                model_calls=model_calls,
                cache_hit_units=cache_hit_units,
                output_written=True,
                execution_result=result,
                batch_state=batch_state,
            )
        _write_completed_batch(output, spec, request, result, batch_state, checkpoint)
        output_written = True
    return _run_result(
        spec,
        output,
        checkpoint,
        reason="all_batches_completed",
        model_calls=model_calls,
        cache_hit_units=cache_hit_units,
        output_written=output_written,
        execution_result=last_execution_result,
        batch_state=last_batch_state,
    )
