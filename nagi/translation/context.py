"""Automatic, bounded translation context; no user reference-file imports."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from ..rag.semantic import RetrievalCache
from .models import RAGEvidenceReference, _canonical_sha256
from .passages import (
    PASSAGE_VERSION,
    NarrativeCorpus,
    bm25_candidates,
    display_text,
    keywords,
    lexical_relevant,
)
from .planner import build_translation_batch_plan
from .terminology import TerminologySnapshot
from .translator import (
    RAGPromptEvidence,
    TranslationAdjacentContext,
    _text_sha256,
    build_translation_request,
)

CONTEXT_VERSION = "translation-context-v3"
DEFAULT_RAG_BUDGET = 3000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class TranslationContextConfig:
    """Internal policy snapshot, not an upload/API configuration format."""

    rag_budget_chars: int = DEFAULT_RAG_BUDGET
    chunk_tokens: int = 320
    overlap_turns: int = 2
    query_tokens: int = 96
    bm25_k: int = 6
    vector_k: int = 10
    rerank_k: int = 8
    per_query_k: int = 2
    batch_max_chunks: int = 4
    # Initial operating points, not probabilities or translation-quality claims.
    semantic_min_score: float = 0.75
    rerank_min_score: float = -4.0
    max_request_bytes: int = 60000
    route_entry_segment_ids: tuple[str, ...] = ()

    def __post_init__(self):
        entries = self.route_entry_segment_ids
        if (
            not isinstance(entries, (tuple, list))
            or any(not isinstance(s, str) or not s for s in entries)
            or len(set(entries)) != len(entries)
        ):
            raise ValueError("route_entry_segment_ids must contain unique segment IDs")
        object.__setattr__(self, "route_entry_segment_ids", tuple(entries))
        limits = {
            "rag_budget_chars": (0, 8000),
            "chunk_tokens": (16, 384),
            "overlap_turns": (0, 2),
            "query_tokens": (16, 96),
            "bm25_k": (1, 20),
            "vector_k": (1, 20),
            "rerank_k": (1, 16),
            "per_query_k": (1, 2),
            "batch_max_chunks": (1, 8),
            "max_request_bytes": (1024, 200000),
        }
        for key, (low, high) in limits.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} must be an integer in [{low}, {high}]")
        for key in ("semantic_min_score", "rerank_min_score"):
            value = getattr(self, key)
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError(f"invalid {key}")
        if not -1 <= self.semantic_min_score <= 1:
            raise ValueError("semantic_min_score must be in [-1, 1]")

    @classmethod
    def from_dict(cls, payload=None):
        if payload is None:
            return cls()
        if not isinstance(payload, dict) or set(payload) - set(
            cls.__dataclass_fields__
        ):
            raise ValueError(
                "unsupported automatic RAG policy; reference imports were removed"
            )
        return cls(**payload)

    def to_dict(self):
        payload = asdict(self)
        payload["route_entry_segment_ids"] = list(self.route_entry_segment_ids)
        return payload


class TranslationContextBuilder:
    def __init__(
        self,
        full_plan,
        config=None,
        *,
        target_language="zh-CN",
        embedder=None,
        reranker=None,
        cache=None,
        progress=None,
        character_glossary=None,
    ):
        if (
            full_plan.status != "ready"
            or full_plan.config.limit is not None
            or full_plan.config.source_limit is not None
            or full_plan.config.selected_segment_ids
            or any(
                key in {"filtered", "limit", "source_limit"} and value
                for key, value in full_plan.skipped_counts
            )
        ):
            raise ValueError("translation context requires a complete validated corpus")
        self.config = TranslationContextConfig.from_dict(
            config.to_dict() if isinstance(config, TranslationContextConfig) else config
        )
        self.narrative = NarrativeCorpus(full_plan, entry_segment_ids=self.config.route_entry_segment_ids)
        self.units = self.narrative.units
        self.target_language = target_language
        self.embedder = embedder
        self.reranker = reranker
        self.cache = cache or RetrievalCache()
        self.character_glossary = character_glossary
        self.progress = progress or (lambda message: None)
        # Byte count is a conservative fallback, NOT a claim to use model tokens.
        self.count_tokens = (
            embedder.count_tokens
            if embedder
            else lambda text: len(text.encode("utf-8"))
        )
        self.passages = self.narrative.passages(
            self.count_tokens,
            target_tokens=self.config.chunk_tokens,
            overlap_turns=self.config.overlap_turns,
        )
        self.index_id = "tctx_v3_" + _canonical_sha256(
            {
                "version": CONTEXT_VERSION,
                "passages": PASSAGE_VERSION,
                "corpus": full_plan.segments_sha256,
                "policy": self.config.to_dict(),
                "embedding": embedder.identity if embedder else None,
                "reranker": reranker.identity if reranker else None,
                "chunks": [p.chunk_id for p in self.passages],
                "target_language": target_language,
                "route_analysis": self.narrative.routes.summary() if self.narrative.routes else None,
                **({"character_glossary_version": character_glossary.version} if character_glossary else {}),
            }
        )
        self.terminology_version = character_glossary.version if character_glossary else "none"
        self.vectors = {}
        if embedder and self.config.rag_budget_chars:
            for offset in range(0, len(self.passages), 32):
                subset = self.passages[offset : offset + 32]
                vectors = self.cache.embeddings(
                    embedder,
                    [p.text for p in subset],
                    policy=f"{PASSAGE_VERSION}:{self.config.chunk_tokens}:{self.config.overlap_turns}",
                )
                self.vectors.update((p.chunk_id, v) for p, v in zip(subset, vectors))
                self.progress(
                    f"本地语义索引：{min(offset + 32, len(self.passages))}/{len(self.passages)} 个片段"
                )

    def summary(self):
        return {
            "index_id": self.index_id,
            "retrieval_mode": "hybrid" if self.embedder else "bm25",
            "embedding_model": self.embedder.identity if self.embedder else None,
            "reranker_model": self.reranker.identity if self.reranker else None,
            "boundary_policy": "must-history-only" if self.narrative.routes else "confirmed-linear-prefix-only",
            "route_analysis": self.narrative.routes.summary() if self.narrative.routes else {
                "status": "unsupported", "reason": "no_control_flow_adapter",
            },
            "chunk_count": len(self.passages),
            "rag_budget_chars_per_batch": self.config.rag_budget_chars,
            "cache_hits": self.cache.hits,
            "cache_misses": self.cache.misses,
        }

    def adjacent(self, unit):
        previous, following = self.narrative.adjacent(unit)
        return TranslationAdjacentContext(
            previous.source_text if previous else None,
            following.source_text if following else None,
        )

    def _query(self, unit):
        parts = [unit]
        if self.count_tokens(display_text(unit)) > self.config.query_tokens:
            return None
        for previous in reversed(self.narrative.previous(unit, 4)):
            candidate = [previous, *parts]
            if (
                self.count_tokens("\n".join(display_text(u) for u in candidate))
                > self.config.query_tokens
            ):
                break
            parts = candidate
        return "\n".join(display_text(u) for u in parts)

    def _retrieve(self, unit, excluded):
        query = self._query(unit)
        if not query:
            return []
        candidates = [
            p for p in self.passages if self.narrative.eligible(p, unit, excluded)
        ]
        if not candidates:
            return []
        lexical = bm25_candidates(
            candidates, query, self.narrative.speakers, self.config.bm25_k
        )
        vector = []
        if self.embedder:
            embedding = self.cache.embeddings(self.embedder, [query], query=True)[0]
            for passage in candidates:
                stored = self.vectors[passage.chunk_id]
                if len(embedding) != len(stored):
                    raise ValueError("query and passage embedding dimensions differ")
                score = sum(a * b for a, b in zip(embedding, stored))
                if score >= self.config.semantic_min_score:
                    vector.append((passage, score))
            vector.sort(key=lambda row: (-row[1], row[0].chunk_id))
            vector = vector[: self.config.vector_k]
        rows = {}
        for route, results in (("bm25", lexical), ("vector", vector)):
            for rank, (passage, _) in enumerate(results, 1):
                row = rows.setdefault(
                    passage.chunk_id, {"passage": passage, "rrf": 0.0, "routes": []}
                )
                row["rrf"] += 1 / (60 + rank)
                row["routes"].append(route)
        ranked = sorted(
            rows.values(), key=lambda r: (-r["rrf"], r["passage"].chunk_id)
        )[: self.config.rerank_k]
        if self.reranker and ranked:
            ranked = [
                row
                for row in ranked
                if self.reranker.fits_pair(query, row["passage"].text)
            ]
            scores = self.cache.scores(
                self.reranker, query, [r["passage"].text for r in ranked]
            )
            for row, score in zip(ranked, scores):
                row["score"] = score
            ranked = [
                row for row in ranked if row["score"] >= self.config.rerank_min_score
            ]
            ranked.sort(key=lambda r: (-r["score"], -r["rrf"], r["passage"].chunk_id))
        else:
            # Without reranking, semantic-only matches require lexical support.
            ranked = [
                row
                for row in ranked
                if "bm25" in row["routes"]
                or lexical_relevant(keywords(query), keywords(row["passage"].text))
            ]
            for row in ranked:
                row["score"] = row["rrf"]
        return ranked[: self.config.per_query_k]

    def _evidence(self, row):
        passage = row["passage"]
        first, last = passage.units[0], passage.units[-1]
        text = _json(
            {
                "kind": "source_passage",
                "chunk_id": passage.chunk_id,
                "source_text": passage.text,
                "scene": passage.scene,
                "history": "all_paths" if self.narrative.routes else "confirmed_prefix",
                "retrieved_by": row["routes"],
                "passage_segment_ids": list(passage.segment_ids),
            }
        )
        return RAGPromptEvidence(
            RAGEvidenceReference(
                self.index_id,
                first.segment_id,
                f"{first.source.archive_name}:{first.source.output_path}:{first.source.line_start}-{last.source.line_end}",
                float(row["score"]),
                _text_sha256(text),
            ),
            text,
        )

    def build_request(self, batch, *, model_id, prompt_version):
        for unit in batch.units:
            original = self.units.get(unit.segment_id)
            if (
                original is None
                or original.source != unit.source
                or original.source_text != unit.source_text
            ):
                raise ValueError("batch does not match full corpus")
        adjacent = {unit.unit_id: self.adjacent(unit) for unit in batch.units}
        excluded = {unit.segment_id for unit in batch.units}
        for unit in batch.units:
            excluded.update(u.segment_id for u in self.narrative.adjacent(unit) if u)
        options = [
            self._retrieve(unit, excluded) if self.config.rag_budget_chars else []
            for unit in batch.units
        ]
        selected = {unit.unit_id: [] for unit in batch.units}
        pooled, seen_passages, seen_texts = {}, set(), set()
        remaining = max(0, self.config.rag_budget_chars - 32)  # Pool wrapper overhead.
        for rank in range(self.config.per_query_k):
            for unit, rows in zip(batch.units, options):
                if rank >= len(rows):
                    continue
                row = rows[rank]
                passage = row["passage"]
                if passage.chunk_id in pooled:
                    if remaining >= 67:
                        selected[unit.unit_id].append(pooled[passage.chunk_id])
                        remaining -= 67  # Quoted SHA-256 reference plus separator.
                    continue
                if (
                    len(pooled) >= self.config.batch_max_chunks
                    or set(passage.segment_ids) & seen_passages
                    or passage.text in seen_texts
                ):
                    continue
                evidence = self._evidence(row)
                cost = (
                    len(_json(evidence.prompt_dict())) + 136
                )  # Pool key and first reference.
                if cost > remaining:
                    continue
                selected[unit.unit_id].append(evidence)
                pooled[passage.chunk_id] = evidence
                seen_passages.update(passage.segment_ids)
                seen_texts.add(passage.text)
                remaining -= cost
        request = build_translation_request(
            batch,
            model_id=model_id,
            prompt_version=prompt_version,
            terminology_version=self.terminology_version,
            rag_index_id=self.index_id,
            target_language=self.target_language,
            terminology=self.character_glossary.qlie_terms(batch.units) if self.character_glossary else (),
            adjacent_context=adjacent,
            rag_context={key: tuple(value) for key, value in selected.items()},
        )
        if len(_json(request.messages).encode("utf-8")) > self.config.max_request_bytes:
            raise ValueError(
                "translation input budget exceeded; use a smaller batch_size"
            )
        return request

    def terminology_snapshot(self):
        return TerminologySnapshot(self.terminology_version)


def prepare_translation_requests(
    corpus_path,
    *,
    model_id,
    prompt_version="qlie-translation-v1",
    target_language="zh-CN",
    config=None,
    batch_size=32,
    limit=None,
    source_limit=None,
    selected_segment_ids=(),
    kinds=(),
    speaker=None,
    scene=None,
    output_path_prefix=None,
    embedder=None,
    reranker=None,
    cache=None,
    progress=None,
    summary=None,
    character_glossary=None,
):
    full_plan = build_translation_batch_plan(
        corpus_path, model_id=model_id, prompt_version=prompt_version
    )
    context = TranslationContextBuilder(
        full_plan,
        config,
        target_language=target_language,
        embedder=embedder,
        reranker=reranker,
        cache=cache,
        progress=progress,
        character_glossary=character_glossary,
    )
    plan = build_translation_batch_plan(
        corpus_path,
        model_id=model_id,
        prompt_version=prompt_version,
        terminology_version=context.terminology_version,
        rag_index_id=context.index_id,
        batch_size=batch_size,
        limit=limit,
        source_limit=source_limit,
        selected_segment_ids=selected_segment_ids,
        kinds=kinds,
        speaker=speaker,
        scene=scene,
        output_path_prefix=output_path_prefix,
        unit_groups=context.narrative.groups,
    )
    if plan.status != "ready" or plan.segments_sha256 != full_plan.segments_sha256:
        raise ValueError("translation selection is invalid or corpus changed")
    requests = []
    for batch in plan.batches:
        context.progress(f"准备翻译上下文：{batch.ordinal}/{len(plan.batches)} 批")
        requests.append(
            context.build_request(
                batch, model_id=model_id, prompt_version=prompt_version
            )
        )
    if summary is not None:
        summary.update(context.summary())
    return plan, tuple(requests), context.terminology_snapshot()
