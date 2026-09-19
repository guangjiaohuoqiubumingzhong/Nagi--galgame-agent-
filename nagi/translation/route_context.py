"""Validated engine dispatch and bounded, per-record must-history evidence."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from .routes import RouteAnalysis


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def analyze_extracted_routes(corpus, *, game_dir=None, entry_ids=()):
    corpus = Path(corpus)
    extraction = json.loads((corpus / "extraction.json").read_text(encoding="utf-8"))
    units = json.loads((corpus / "texts.json").read_text(encoding="utf-8"))
    engine = extraction.get("engine")
    if extraction.get("text_count") != len(units) or len(
        {u["id"] for u in units}
    ) != len(units):
        raise ValueError("Route corpus count or IDs changed; extract again")
    if engine == "yuris-479":
        from ..gameio.yuris import archive_entries, catalog, load_source
        from ..gameio.yuris_flow import build_program

        source, _ = load_source(corpus, game_dir or extraction.get("game_root"))
        if hashlib.sha256(source).hexdigest() != extraction["archive_sha256"]:
            raise ValueError("YU-RIS source archive changed; extract again")
        entries = archive_entries(source)
        table, key, expected = catalog(entries)
        if units != expected:
            raise ValueError("YU-RIS route corpus differs from source")
        program = build_program(entries, table, key, units, entry_ids=entry_ids)
    elif engine in {"renpy", "kirikiri", "tyranoscript"}:
        from ..gameio.text_engines import TOKEN_RE, _ks_spans, _renpy_spans

        export = (corpus.parent / "script-export").resolve()
        documents, storage, expected = {}, {}, []
        for document in extraction["documents"]:
            path = (export / document["output_relative"]).resolve()
            if export not in path.parents:
                raise ValueError("Route script escapes export directory")
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            snapshot_hash = document.get("text_sha256")
            if snapshot_hash:
                valid = hashlib.sha256(raw).hexdigest() == snapshot_hash
            else:
                # Legacy exports can be verified only when encoding round-trips.
                valid = (
                    hashlib.sha256(text.encode(document["encoding"])).hexdigest()
                    == document["source_sha256"]
                )
            if not valid:
                raise ValueError("Route script snapshot changed; extract again")
            key = document["key"]
            if key in documents:
                raise ValueError("Duplicate route document")
            documents[key] = text
            logical = (
                document.get("internal_path") or document["source_relative"]
            ).replace("\\", "/")
            if engine == "tyranoscript" and logical.startswith("data/scenario/"):
                logical = logical[len("data/scenario/") :]
            if logical in storage:
                raise ValueError(
                    "Ambiguous script resource overrides; route resolution requires unique storage names"
                )
            storage[logical] = (key, text)
            spans = _renpy_spans(text) if engine == "renpy" else _ks_spans(text)
            for start, end, value, source_text in spans:
                sid = hashlib.sha256(
                    f"{key}:{start}:{end}:{value}".encode()
                ).hexdigest()[:24]
                expected.append(
                    {
                        "id": sid,
                        "text": value,
                        "document": key,
                        "source_text": source_text,
                        "start": start,
                        "end": end,
                        "tokens": TOKEN_RE.findall(value),
                    }
                )
        if expected != units:
            raise ValueError("Route text spans differ from validated script snapshots")
        if engine == "renpy":
            from ..gameio.renpy_flow import build_program

            program = build_program(documents, units, entry_ids=entry_ids)
        else:
            from ..gameio.kag_flow import build_program

            program = build_program(storage, units, engine=engine, entry_ids=entry_ids)
    else:
        raise ValueError(f"No extracted route adapter for {engine!r}")
    return RouteAnalysis(program, [u["id"] for u in units]), units


class RouteContext:
    def __init__(self, analysis, units, *, embedder=None, reranker=None,
                 cache=None, config=None, legacy=False, progress=None):
        from ..rag.semantic import RetrievalCache
        from .context import TranslationContextConfig

        self.analysis = analysis
        self.units = {u["id"]: u for u in units}
        self.legacy = legacy
        self.config = config or TranslationContextConfig()
        self.embedder = embedder
        self.reranker = reranker
        self.cache = cache or RetrievalCache()
        self.vectors = {}
        self.identity = digest(
            {
                "policy": "must-bm25-v1",
                "program": asdict(analysis.program),
                "analysis": analysis.summary(),
                "units": units,
            }
        )
        if not legacy:
            self.identity = "route_hybrid_v1_" + digest({
                "source": self.identity,
                "config": self.config.to_dict(),
                "embedding": embedder.identity if embedder else None,
                "reranker": reranker.identity if reranker else None,
            })
        # Whole records stay intact. Oversized records retain the lexical route
        # rather than being silently truncated by the neural tokenizer.
        if embedder and self.config.rag_budget_chars and not legacy:
            passages = [u for u in units if u.get("command", "WORD") == "WORD"
                        and embedder.count_tokens(u["text"]) <= self.config.chunk_tokens]
            for offset in range(0, len(passages), 32):
                if progress:
                    progress(f"本地语义索引：{offset}/{len(passages)} 条文本")
                subset = passages[offset:offset + 32]
                vectors = self.cache.embeddings(
                    embedder, [u["text"] for u in subset], policy="route-records-v1",
                )
                self.vectors.update((u["id"], v) for u, v in zip(subset, vectors))

    def summary(self):
        return {
            "boundary_policy": "must-history-only",
            "route_context_id": self.identity,
            "route_analysis": self.analysis.summary(),
            "retrieval_mode": "hybrid" if self.embedder and not self.legacy else "bm25",
            "embedding_model": self.embedder.identity if self.embedder else None,
            "reranker_model": self.reranker.identity if self.reranker else None,
            "rag_budget_chars_per_batch": 3000 if self.legacy else self.config.rag_budget_chars,
            "semantic_record_count": len(self.vectors),
        }

    def payload(self, batch):
        from .passages import bm25_candidates
        from .retrieval import retrieve_history

        excluded = {u["id"] for u in batch}
        pool, references = {}, {}
        if not self.legacy and not self.config.rag_budget_chars:
            return {"evidence_pool": [], "references": {}}
        for unit in batch:
            _, must = self.analysis.history_masks(unit["id"])
            candidates = [
                SimpleNamespace(chunk_id=sid, text=self.units[sid]["text"])
                for sid, bit in self.analysis.bits.items()
                if bit & must
                and sid not in excluded
                and self.units[sid].get("command", "WORD") == "WORD"
            ]
            if self.legacy:
                selected = [p for p, _ in bm25_candidates(candidates, unit["text"], (), limit=2)]
            else:
                embedder = self.embedder
                if embedder and embedder.count_tokens(unit["text"]) > self.config.query_tokens:
                    embedder = None
                selected = [row["passage"] for row in retrieve_history(
                    unit["text"], candidates, config=self.config, embedder=embedder,
                    reranker=self.reranker, cache=self.cache, vectors=self.vectors,
                )] if candidates else []
            for candidate in selected:
                sid = candidate.chunk_id
                evidence = {"id": sid, "text": candidate.text, "history": "all_paths"}
                proposed = dict(pool)
                proposed[sid] = evidence
                refs = {key: list(value) for key, value in references.items()}
                refs.setdefault(unit["id"], []).append(sid)
                payload = {"evidence_pool": list(proposed.values()), "references": refs}
                if (
                    len(proposed) <= (4 if self.legacy else self.config.batch_max_chunks)
                    and len(json.dumps(payload, ensure_ascii=False))
                    <= (3000 if self.legacy else self.config.rag_budget_chars)
                ):
                    pool, references = proposed, refs
        return {"evidence_pool": list(pool.values()), "references": references}


def prepare_route_context(corpus, previous, *, game_dir=None, job=None):
    # Never alter messages for previously paid tasks created before this feature.
    if previous is not None and "route_context_id" not in previous:
        return None
    from ..paths import state_root
    from ..rag.semantic import RetrievalCache, local_models
    from .context import TranslationContextConfig

    legacy = previous is not None and not previous["route_context_id"].startswith("route_hybrid_v1_")
    config = getattr(job, "context_config", None) or TranslationContextConfig()
    analysis, units = analyze_extracted_routes(
        corpus, game_dir=game_dir,
        entry_ids=() if legacy else config.route_entry_segment_ids,
    )
    embedder, reranker, missing = None, None, ()
    if not legacy:
        models = getattr(job, "retrieval_models", None)
        if models is None:
            models = local_models()
            if job is not None:
                job.retrieval_models = models
        embedder, reranker, missing = models

    def progress(message):
        if job is not None:
            if job.cancel_event.is_set():
                raise InterruptedError("translation context cancelled")
            job.update(message=message)

    context = RouteContext(
        analysis, units, embedder=embedder, reranker=reranker, config=config,
        legacy=legacy, progress=progress,
        cache=RetrievalCache(state_root() / "rag-cache" / "retrieval.sqlite3")
        if embedder or reranker else None,
    )
    if previous and previous["route_context_id"] != context.identity:
        raise ValueError("Saved route context differs; start a new translation run")
    if job is not None:
        job.update(context_summary={**context.summary(), "missing_models": list(missing)})
    return context
