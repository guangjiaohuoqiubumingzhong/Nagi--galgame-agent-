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
    def __init__(self, analysis, units):
        self.analysis = analysis
        self.units = {u["id"]: u for u in units}
        self.identity = digest(
            {
                "policy": "must-bm25-v1",
                "program": asdict(analysis.program),
                "analysis": analysis.summary(),
                "units": units,
            }
        )

    def summary(self):
        return {
            "boundary_policy": "must-history-only",
            "route_context_id": self.identity,
            "route_analysis": self.analysis.summary(),
        }

    def payload(self, batch):
        from .passages import bm25_candidates

        excluded = {u["id"] for u in batch}
        pool, references = {}, {}
        for unit in batch:
            _, must = self.analysis.history_masks(unit["id"])
            candidates = [
                SimpleNamespace(chunk_id=sid, text=self.units[sid]["text"])
                for sid, bit in self.analysis.bits.items()
                if bit & must
                and sid not in excluded
                and self.units[sid].get("command", "WORD") == "WORD"
            ]
            for candidate, _ in bm25_candidates(candidates, unit["text"], (), limit=2):
                sid = candidate.chunk_id
                evidence = {"id": sid, "text": candidate.text, "history": "all_paths"}
                proposed = dict(pool)
                proposed[sid] = evidence
                refs = {key: list(value) for key, value in references.items()}
                refs.setdefault(unit["id"], []).append(sid)
                payload = {"evidence_pool": list(proposed.values()), "references": refs}
                if (
                    len(proposed) <= 4
                    and len(json.dumps(payload, ensure_ascii=False)) <= 3000
                ):
                    pool, references = proposed, refs
        return {"evidence_pool": list(pool.values()), "references": references}


def prepare_route_context(corpus, previous, *, game_dir=None):
    # Never alter messages for previously paid tasks created before this feature.
    if previous is not None and "route_context_id" not in previous:
        return None
    analysis, units = analyze_extracted_routes(corpus, game_dir=game_dir)
    context = RouteContext(analysis, units)
    if previous and previous["route_context_id"] != context.identity:
        raise ValueError("Saved route context differs; start a new translation run")
    return context
