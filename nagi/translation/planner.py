"""Read-only, deterministic planning from Segment v1 corpus to translation batches."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..gameio.segments import (
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_SEGMENTS,
    SegmentContractError,
    TextSegment,
)
from .models import (
    BATCH_ID_PREFIX,
    PLAN_ID_PREFIX,
    TRANSLATION_PLANNER_VERSION,
    TRANSLATION_SCHEMA_VERSION,
    TranslationBatch,
    TranslationSourceReference,
    TranslationUnit,
    _canonical_sha256,
    _nonempty,
    make_translation_cache_key,
    make_translation_unit_id,
)

MAX_CORPUS_BYTES = 1024 * 1024 * 1024
MAX_PARSE_REPORT_BYTES = 32 * 1024 * 1024
DEFAULT_BATCH_SIZE = 32
MAX_BATCH_SIZE = 1000
MAX_PLAN_UNITS = 1_000_000
SAFE_REVIEW_STATUSES = frozenset({"accepted", "not_required"})


def _normalized_equal(left, right):
    return unicodedata.normalize("NFKC", str(left)).casefold() == unicodedata.normalize(
        "NFKC", str(right)
    ).casefold()


@dataclass(frozen=True)
class TranslationPlannerConfig:
    model_id: str = "dry-run-model"
    prompt_version: str = "qlie-translation-v1"
    terminology_version: str = "none"
    rag_index_id: str = "none"
    batch_size: int = DEFAULT_BATCH_SIZE
    limit: int | None = None
    source_limit: int | None = None
    selected_segment_ids: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    speaker: str | None = None
    scene: str | None = None
    output_path_prefix: str | None = None

    def __post_init__(self):
        if (not isinstance(self.selected_segment_ids, tuple)
                or any(not isinstance(value, str) or not value for value in self.selected_segment_ids)
                or len(set(self.selected_segment_ids)) != len(self.selected_segment_ids)):
            raise ValueError("selected_segment_ids must contain unique non-empty segment IDs")
        for value, field_name in (
            (self.model_id, "model_id"),
            (self.prompt_version, "prompt_version"),
            (self.terminology_version, "terminology_version"),
            (self.rag_index_id, "rag_index_id"),
        ):
            _nonempty(value, field_name)
        if (
            isinstance(self.batch_size, bool)
            or not isinstance(self.batch_size, int)
            or not 1 <= self.batch_size <= MAX_BATCH_SIZE
        ):
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if self.limit is not None and (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_PLAN_UNITS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_PLAN_UNITS}")
        if not isinstance(self.kinds, tuple) or any(
            not isinstance(value, str) or not value for value in self.kinds
        ):
            raise ValueError("kinds must be a tuple of non-empty strings")
        if self.source_limit is not None and (
            isinstance(self.source_limit, bool)
            or not isinstance(self.source_limit, int)
            or not 1 <= self.source_limit <= MAX_PLAN_UNITS
        ):
            raise ValueError(f"source_limit must be between 1 and {MAX_PLAN_UNITS}")
        for value, field_name in (
            (self.speaker, "speaker"),
            (self.scene, "scene"),
            (self.output_path_prefix, "output_path_prefix"),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string or null")

    def to_dict(self):
        return {
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "terminology_version": self.terminology_version,
            "rag_index_id": self.rag_index_id,
            "batch_size": self.batch_size,
            "limit": self.limit,
            "kinds": list(self.kinds),
            "speaker": self.speaker,
            "scene": self.scene,
            "output_path_prefix": self.output_path_prefix,
            **({"source_limit": self.source_limit} if self.source_limit is not None else {}),
            **({"selected_segment_ids": list(self.selected_segment_ids)} if self.selected_segment_ids else {}),
        }


@dataclass(frozen=True)
class TranslationBatchPlan:
    status: str
    reason: str
    plan_id: str | None
    corpus_dir: str
    segments_path: str
    segments_sha256: str | None
    parse_report_path: str
    parse_report_sha256: str | None
    config: TranslationPlannerConfig
    segment_count: int = 0
    selected_unit_count: int = 0
    skipped_counts: tuple[tuple[str, int], ...] = ()
    kind_counts: tuple[tuple[str, int], ...] = ()
    batches: tuple[TranslationBatch, ...] = field(default=(), repr=False)
    warnings: tuple[str, ...] = ()

    def to_dict(self, *, include_text=False):
        return {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "planner_version": TRANSLATION_PLANNER_VERSION,
            "status": self.status,
            "reason": self.reason,
            "plan_id": self.plan_id,
            "source": {
                "corpus_dir": self.corpus_dir,
                "segments_path": self.segments_path,
                "segments_sha256": self.segments_sha256,
                "parse_report_path": self.parse_report_path,
                "parse_report_sha256": self.parse_report_sha256,
            },
            "config": self.config.to_dict(),
            "summary": {
                "segment_count": self.segment_count,
                "selected_unit_count": self.selected_unit_count,
                "batch_count": len(self.batches),
                "skipped_counts": dict(self.skipped_counts),
                "kind_counts": dict(self.kind_counts),
            },
            "batches": [batch.to_dict(include_text=include_text) for batch in self.batches],
            "side_effects": {
                "model_called": False,
                "output_written": False,
                "game_modified": False,
            },
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(include_text=False), ensure_ascii=False, indent=2, sort_keys=True)


def _invalid_plan(corpus_path, config, reason):
    source = Path(corpus_path).expanduser().resolve()
    root = source if source.is_dir() else source.parent
    segments = source if source.is_file() else root / "segments.jsonl"
    report = root / "parse-report.json"
    return TranslationBatchPlan(
        status="invalid_input",
        reason=str(reason),
        plan_id=None,
        corpus_dir=str(root),
        segments_path=str(segments),
        segments_sha256=None,
        parse_report_path=str(report),
        parse_report_sha256=None,
        config=config,
    )


def _resolve_corpus(corpus_path):
    source = Path(corpus_path).expanduser().resolve()
    root = source if source.is_dir() else source.parent
    segments_path = root / "segments.jsonl" if source.is_dir() else source
    report_path = root / "parse-report.json"
    for path, label, size_limit in (
        (segments_path, "segments.jsonl", MAX_CORPUS_BYTES),
        (report_path, "parse-report.json", MAX_PARSE_REPORT_BYTES),
    ):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} is missing or not a regular file")
        if path.stat().st_size > size_limit:
            raise ValueError(f"{label} exceeds the translation planning size limit")
    report_bytes = report_path.read_bytes()
    try:
        report = json.loads(report_bytes.decode("utf-8-sig"))
        if report["status"] != "published":
            raise ValueError("parse-report.json is not a published corpus report")
        expected_hash = report["artifacts"]["segments"]["sha256"]
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("parse-report.json contains an invalid segments hash")
        summary = report["summary"]
        expected_count = summary["segment_count"]
        expected_unknown_count = summary["unknown_count"]
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 1
            or isinstance(expected_unknown_count, bool)
            or not isinstance(expected_unknown_count, int)
            or expected_unknown_count < 0
        ):
            raise ValueError("parse-report.json contains invalid corpus counters")
        review = report.get("unknown_review")
        if expected_unknown_count:
            if not isinstance(review, dict) or review.get("status") != "accepted":
                raise ValueError("published corpus unknown review is missing or not accepted")
            if (
                review.get("expected_count") != expected_unknown_count
                or review.get("reviewed_count") != expected_unknown_count
            ):
                raise ValueError("published corpus unknown review is incomplete")
        elif isinstance(review, dict) and review.get("status") not in SAFE_REVIEW_STATUSES:
            raise ValueError("published corpus unknown review status is unsafe")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"parse-report.json is invalid: {exc}") from exc
    return (
        root,
        segments_path,
        report_path,
        hashlib.sha256(report_bytes).hexdigest(),
        expected_hash.casefold(),
        expected_count,
        expected_unknown_count,
    )


def _matches_config(segment, config):
    if config.kinds and segment.kind not in config.kinds:
        return False
    if config.speaker is not None and (
        segment.speaker is None or not _normalized_equal(segment.speaker, config.speaker)
    ):
        return False
    if config.scene is not None and (
        segment.scene is None or not _normalized_equal(segment.scene, config.scene)
    ):
        return False
    if config.output_path_prefix is not None:
        prefix = config.output_path_prefix.replace("\\", "/").casefold().rstrip("/")
        output_path = segment.source.output_path.replace("\\", "/").casefold()
        if output_path != prefix and not output_path.startswith(prefix + "/"):
            return False
    return True


def _unit_from_segment(segment, config):
    unit_id = make_translation_unit_id(segment)
    cache_key = make_translation_cache_key(
        unit_id=unit_id,
        source_text_sha256=segment.source_text_sha256,
        model_id=config.model_id,
        prompt_version=config.prompt_version,
        terminology_version=config.terminology_version,
        rag_index_id=config.rag_index_id,
        rag_evidence=(),
    )
    return TranslationUnit(
        unit_id=unit_id,
        segment_id=segment.segment_id,
        cache_key=cache_key,
        kind=segment.kind,
        speaker=segment.speaker,
        scene=segment.scene,
        source_text=segment.source_text,
        normalized_text=segment.normalized_text,
        source_text_sha256=segment.source_text_sha256,
        placeholders=segment.placeholders,
        tags=segment.tags,
        previous_segment_id=segment.previous_segment_id,
        next_segment_id=segment.next_segment_id,
        source=TranslationSourceReference.from_segment(segment),
        rag_evidence=(),
    )


def _build_batches(units, corpus_sha256, config, unit_groups=None):
    batches = []
    groups = []
    for unit in units:
        if (not groups or len(groups[-1]) >= config.batch_size or
                unit_groups is not None and unit_groups[unit.segment_id] != unit_groups[groups[-1][-1].segment_id]):
            groups.append([])
        groups[-1].append(unit)
    for group in groups:
        batch_units = tuple(group)
        ordinal = len(batches) + 1
        batch_id = BATCH_ID_PREFIX + _canonical_sha256(
            {
                "planner_version": TRANSLATION_PLANNER_VERSION,
                "corpus_sha256": corpus_sha256,
                "config": config.to_dict(),
                "ordinal": ordinal,
                "cache_keys": [unit.cache_key for unit in batch_units],
            }
        )
        batches.append(
            TranslationBatch(
                batch_id=batch_id,
                ordinal=ordinal,
                status="planned",
                units=batch_units,
            )
        )
    return tuple(batches)


def build_translation_batch_plan(
    corpus_path,
    *,
    model_id="dry-run-model",
    prompt_version="qlie-translation-v1",
    terminology_version="none",
    rag_index_id="none",
    batch_size=DEFAULT_BATCH_SIZE,
    limit=None,
    source_limit=None,
    selected_segment_ids=(),
    kinds=(),
    speaker=None,
    scene=None,
    output_path_prefix=None,
    unit_groups=None,
):
    try:
        config = TranslationPlannerConfig(
            model_id=model_id,
            prompt_version=prompt_version,
            terminology_version=terminology_version,
            rag_index_id=rag_index_id,
            batch_size=batch_size,
            limit=limit,
            source_limit=source_limit,
            selected_segment_ids=tuple(selected_segment_ids),
            kinds=tuple(kinds or ()),
            speaker=speaker,
            scene=scene,
            output_path_prefix=output_path_prefix,
        )
    except (TypeError, ValueError) as exc:
        fallback = TranslationPlannerConfig()
        return _invalid_plan(corpus_path, fallback, exc)
    try:
        (
            root,
            segments_path,
            report_path,
            report_sha256,
            expected_hash,
            expected_count,
            expected_unknown_count,
        ) = _resolve_corpus(corpus_path)
        digest = hashlib.sha256()
        links = {}
        units = []
        kind_counts = Counter()
        skipped = Counter()
        segment_count = 0
        selected_ids = set(config.selected_segment_ids)
        with segments_path.open("rb") as stream:
            for line_number, raw in enumerate(stream, start=1):
                if len(raw) > MAX_JSONL_LINE_BYTES:
                    raise ValueError(f"Segment v1 line {line_number} exceeds the protocol limit")
                digest.update(raw)
                if not raw.endswith(b"\n"):
                    raise ValueError("segments.jsonl must end every record with LF")
                try:
                    segment = TextSegment.from_dict(json.loads(raw))
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    SegmentContractError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise ValueError(f"invalid Segment v1 record at line {line_number}: {exc}") from exc
                segment_count += 1
                if segment_count > MAX_JSONL_SEGMENTS:
                    raise ValueError("segments.jsonl exceeds the Segment v1 count limit")
                if segment.segment_id in links:
                    raise ValueError("segments.jsonl contains duplicate segment IDs")
                links[segment.segment_id] = (
                    segment.previous_segment_id,
                    segment.next_segment_id,
                )
                kind_counts[segment.kind] += 1
                outside_scope = config.source_limit is not None and segment_count > config.source_limit
                if segment.kind == "unknown":
                    skipped["unknown"] += 1
                    continue
                if not segment.translatable:
                    skipped["nontranslatable"] += 1
                    continue
                if outside_scope:
                    skipped["source_limit"] += 1
                    continue
                if not _matches_config(segment, config):
                    skipped["filtered"] += 1
                    continue
                if selected_ids and segment.segment_id not in selected_ids:
                    skipped["opening_selection"] += 1
                    continue
                if config.limit is not None and len(units) >= config.limit:
                    skipped["limit"] += 1
                    continue
                units.append(_unit_from_segment(segment, config))
        if segment_count != expected_count:
            raise ValueError("segments.jsonl count does not match parse-report.json")
        if kind_counts.get("unknown", 0) != expected_unknown_count:
            raise ValueError("unknown segment count does not match parse-report.json")
        corpus_sha256 = digest.hexdigest()
        if corpus_sha256 != expected_hash:
            raise ValueError("segments.jsonl hash does not match parse-report.json")
        if selected_ids:
            by_id = {unit.segment_id: unit for unit in units}
            if set(by_id) != selected_ids:
                raise ValueError("opening selection IDs do not match the validated translation units")
            units = [by_id[identifier] for identifier in config.selected_segment_ids]
        for segment_id, (previous, following) in links.items():
            if previous is not None and (
                previous not in links or links[previous][1] != segment_id
            ):
                raise ValueError("segments.jsonl contains a missing or non-reciprocal previous link")
            if following is not None and (
                following not in links or links[following][0] != segment_id
            ):
                raise ValueError("segments.jsonl contains a missing or non-reciprocal next link")
        batches = _build_batches(units, corpus_sha256, config, unit_groups)
        plan_id = PLAN_ID_PREFIX + _canonical_sha256(
            {
                "planner_version": TRANSLATION_PLANNER_VERSION,
                "corpus_sha256": corpus_sha256,
                "parse_report_sha256": report_sha256,
                "config": config.to_dict(),
                "batch_ids": [batch.batch_id for batch in batches],
            }
        )
        status = "ready" if batches else "empty"
        reason = (
            "translation units are ready for a future explicit model execution"
            if batches
            else "valid corpus contains no translation units matching the selection"
        )
        return TranslationBatchPlan(
            status=status,
            reason=reason,
            plan_id=plan_id,
            corpus_dir=str(root),
            segments_path=str(segments_path),
            segments_sha256=corpus_sha256,
            parse_report_path=str(report_path),
            parse_report_sha256=report_sha256,
            config=config,
            segment_count=segment_count,
            selected_unit_count=len(units),
            skipped_counts=tuple(sorted(skipped.items())),
            kind_counts=tuple(sorted(kind_counts.items())),
            batches=batches,
        )
    except (OSError, TypeError, ValueError) as exc:
        return _invalid_plan(corpus_path, config, exc)


def render_translation_batch_plan_text(plan):
    skipped = dict(plan.skipped_counts)
    return "\n".join(
        [
            f"Translation batch plan: {plan.status}",
            f"corpus: {plan.corpus_dir}",
            f"plan_id: {plan.plan_id or 'not available'}",
            (
                f"segments: {plan.segment_count}; units: {plan.selected_unit_count}; "
                f"batches: {len(plan.batches)}"
            ),
            (
                f"skipped: unknown={skipped.get('unknown', 0)}; "
                f"nontranslatable={skipped.get('nontranslatable', 0)}; "
                f"filtered={skipped.get('filtered', 0)}; limit={skipped.get('limit', 0)}"
            ),
            "side_effects: model_called=false; output_written=false; game_modified=false",
            f"reason: {plan.reason}",
        ]
    )
