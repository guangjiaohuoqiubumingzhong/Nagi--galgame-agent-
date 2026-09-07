import hashlib
import json
import sqlite3
from itertools import pairwise

import pytest

from nagi.gameio.segments import SegmentSource, build_text_segment, segments_to_jsonl
from nagi.rag.semantic import RetrievalCache
from nagi.translation import (
    build_translation_run_spec,
    initialize_translation_run,
    load_translation_checkpoint,
)
from nagi.translation.context import (
    TranslationContextBuilder,
    TranslationContextConfig,
    prepare_translation_requests,
)
from nagi.translation.passages import NarrativeCorpus, entities
from nagi.translation.planner import build_translation_batch_plan


def corpus_fixture(tmp_path, rows=None):
    root = tmp_path / "corpus"
    root.mkdir()
    texts = [
        "Find the silver key to the library gate.",
        "The library gate is closed tonight.",
        "We should leave after sunset.",
        "I cannot stay here any longer.",
        "The silver key opens the library gate beneath the tower.",
        "Remember the silver key beside the library gate.",
        "The rocket travels past Jupiter.",
        "The silver key opens the library gate in the secret ending.",
        "The silver key opens the library gate in the common chapter.",
    ]
    rows = rows or [
        {
            "text": text,
            "path": "scenario/main.s" if i < 7 else f"scenario/route-{i}.s",
            "speaker": "Mira" if i != 6 else "Pilot",
        }
        for i, text in enumerate(texts)
    ]
    segments = []
    for i, row in enumerate(rows):
        raw = row["text"].encode()
        path = row.get("path", "scenario/main.s")
        source = SegmentSource(
            engine="synthetic",
            archive_name="data0.pack",
            internal_path=path,
            output_path=path,
            entry_index=i,
            conflict_group=None,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_size_bytes=len(raw),
            encoding="utf-8",
            byte_start=0,
            byte_end=len(raw),
            line_start=i + 1,
            line_end=i + 1,
        )
        kind = row.get("kind", "dialogue")
        segments.append(
            build_text_segment(
                kind=kind,
                source=source,
                source_text=row["text"],
                translatable=kind not in {"control", "label", "comment", "unknown"},
                speaker=row.get("speaker", "Mira"),
                scene=row.get("scene", "library"),
            )
        )
    raw = segments_to_jsonl(segments).encode()
    (root / "segments.jsonl").write_bytes(raw)
    unknown = sum(s.kind == "unknown" for s in segments)
    (root / "parse-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "published",
                "summary": {"segment_count": len(segments), "unknown_count": unknown},
                "unknown_review": {
                    "status": "accepted" if unknown else "not_required",
                    "expected_count": unknown,
                    "reviewed_count": unknown,
                },
                "artifacts": {
                    "segments": {
                        "path": "segments.jsonl",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root, segments


def prepare(root, config=None, **kwargs):
    return prepare_translation_requests(root, model_id="fake", config=config, **kwargs)


def evidence(request):
    return [item for unit in request.units for item in unit.rag_context]


class FakeEmbedder:
    identity = "test-semantic-v1"

    def __init__(self):
        self.calls = []

    def count_tokens(self, text):
        return len(text.split())

    def embed(self, texts, *, query=False):
        self.calls.append((query, list(texts)))
        return [
            [1.0, 0.0] if "silver key" in t or "promise" in t else [0.0, 1.0]
            for t in texts
        ]


class FakeReranker:
    identity = "test-reranker-v1"

    def __init__(self):
        self.calls = []

    def fits_pair(self, query, passage):
        return True

    def rerank(self, query, passages):
        self.calls.append((query, list(passages)))
        return [2.0 if "silver key" in t else -9.0 for t in passages]


def test_first_pilot_cannot_retrieve_future_even_though_full_corpus_indexed(tmp_path):
    root, segments = corpus_fixture(tmp_path)
    before = {p: p.read_bytes() for p in root.iterdir()}
    summary = {}
    plan, requests, _ = prepare(root, limit=1, summary=summary)
    assert plan.config.rag_index_id.startswith("tctx_v3_")
    assert summary["chunk_count"] > 0
    assert summary["retrieval_mode"] == "bm25"
    assert requests[0].units[0].adjacent_context.next_text == segments[1].source_text
    assert not evidence(requests[0])
    assert before == {p: p.read_bytes() for p in root.iterdir()}


def test_later_unit_retrieves_only_previous_nonadjacent_same_script(tmp_path):
    root, segments = corpus_fixture(tmp_path)
    _, requests, _ = prepare(root, {"chunk_tokens": 80}, batch_size=1)
    selected = evidence(requests[5])
    assert selected
    for item in selected:
        ids = set(json.loads(item.text)["passage_segment_ids"])
        assert ids <= {s.segment_id for s in segments[:4]}
        assert "secret ending" not in item.text and "common chapter" not in item.text
        assert (
            item.reference.content_sha256
            == hashlib.sha256(item.text.encode()).hexdigest()
        )


@pytest.mark.parametrize(
    "barrier",
    [
        {"kind": "control", "text": "^jump,other"},
        {"kind": "control", "text": "$flag=1"},
        {"kind": "choice", "text": "Enter the secret route"},
        {"kind": "unknown", "text": "unrecognized syntax"},
        {"kind": "label", "text": "@@alternative"},
    ],
)
def test_unknown_branches_and_control_flow_are_hard_boundaries(tmp_path, barrier):
    rows = [
        {"text": "The silver key opens the library gate."},
        barrier,
        {"text": "A cloudy night."},
        {"text": "Mira waits."},
        {"text": "Remember the silver key to the library gate."},
    ]
    root, _ = corpus_fixture(tmp_path, rows)
    _, requests, _ = prepare(root, {"chunk_tokens": 80}, batch_size=32)
    assert len(requests) >= 2
    assert not evidence(requests[-1])


def test_scene_boundaries_and_adjacent_context_do_not_cross(tmp_path):
    root, _ = corpus_fixture(
        tmp_path,
        [
            {"text": "The promise was made at the cafe.", "scene": "A"},
            {"text": "She remembers the promise.", "scene": "B"},
        ],
    )
    _, requests, _ = prepare(root)
    assert len(requests) == 2
    assert requests[0].units[0].adjacent_context.next_text is None
    assert requests[1].units[0].adjacent_context.previous_text is None


def test_chunks_preserve_turns_bound_size_overlap_and_never_cross_scope(tmp_path):
    root, _ = corpus_fixture(
        tmp_path,
        [
            {"text": f"word{i} one two three four", "scene": "A" if i < 12 else "B"}
            for i in range(20)
        ],
    )
    narrative = NarrativeCorpus(build_translation_batch_plan(root))
    passages = narrative.passages(
        lambda t: len(t.split()), target_tokens=32, overlap_turns=2
    )
    assert len(passages) > 2
    for p in passages:
        assert p.token_count <= 32
        assert len({u.scene for u in p.units}) == 1
        assert all(u.source_text in p.text for u in p.units)
    for left, right in pairwise(passages):
        overlap = set(left.segment_ids) & set(right.segment_ids)
        assert len(overlap) <= 2
        if overlap:
            assert left.scope == right.scope
            assert (
                sum(
                    len(u.source_text.split()) + 1
                    for u in left.units
                    if u.segment_id in overlap
                )
                <= left.token_count * 0.2
            )


def test_entity_matching_does_not_invent_aliases():
    found = entities("美咲は月影学園で待つ。", ("美咲", "美月"))
    assert "美咲" in found and "美月" not in found
    assert any("月影学園" in name for name in found)
    assert "凛" in entities("凛は待っている。", ("凛",))
    assert "mira" in entities("Ｍｉｒａ", ("Mira",))


def test_semantic_only_candidate_can_survive_rerank(tmp_path):
    rows = [
        {"text": "Find the silver key near the old library gate today."},
        {"text": "The weather is cold and wind blows through empty streets."},
        {"text": "My bicycle has a broken wheel and needs new paint."},
        {"text": "The train is late and passengers wait on the platform."},
        {
            "text": "Yesterday our promise involved a precious object opening an entrance."
        },
    ]
    root, segments = corpus_fixture(tmp_path, rows)
    embedder, reranker = FakeEmbedder(), FakeReranker()
    _, requests, _ = prepare(
        root,
        {"chunk_tokens": 16, "query_tokens": 16},
        batch_size=1,
        embedder=embedder,
        reranker=reranker,
    )
    found = evidence(requests[-1])
    assert found
    assert segments[0].segment_id in json.loads(found[0].text)["passage_segment_ids"]
    assert "vector" in json.loads(found[0].text)["retrieved_by"]
    assert reranker.calls and max(len(p) for _, p in reranker.calls) <= 8


def test_rerank_can_reject_every_candidate(tmp_path):
    root, _ = corpus_fixture(tmp_path)

    class RejectAll(FakeReranker):
        def rerank(self, query, passages):
            return [-99.0] * len(passages)

    _, requests, _ = prepare(
        root,
        {"chunk_tokens": 16},
        batch_size=1,
        embedder=FakeEmbedder(),
        reranker=RejectAll(),
    )
    assert not any(evidence(r) for r in requests)


@pytest.mark.parametrize("budget", [0, 1, 100, 750, 3000])
def test_pool_and_references_fit_whole_batch_budget(tmp_path, budget):
    root, _ = corpus_fixture(tmp_path)
    _, requests, _ = prepare(
        root, {"chunk_tokens": 80, "rag_budget_chars": budget}, batch_size=1
    )
    for request in requests:
        payload = json.loads(
            request.messages[1]["content"].split("REQUEST_JSON:", 1)[1]
        )
        pool = payload.get("rag_evidence_pool", {})
        references = [row.get("rag_evidence_ids", []) for row in payload["units"]]
        if pool:
            cost = len(json.dumps(pool, ensure_ascii=False, separators=(",", ":")))
            cost += sum(
                len(json.dumps(ids, separators=(",", ":"))) for ids in references
            )
            assert cost <= budget
        assert len(pool) <= 4 and all(len(ids) <= 2 for ids in references)


@pytest.mark.parametrize("index_prefix", ["tctx_v2_", "tctx_v3_"])
def test_shared_evidence_serializes_once_and_each_unit_references_it(tmp_path, index_prefix):
    from nagi.translation.models import TranslationBatch

    root, _ = corpus_fixture(tmp_path)
    full = build_translation_batch_plan(root)
    builder = TranslationContextBuilder(full, {"chunk_tokens": 80})
    builder.index_id = index_prefix + builder.index_id.removeprefix("tctx_v3_")
    units = list(builder.units.values())
    batch = TranslationBatch("tb_v1_test", 1, "planned", tuple(units[5:7]))
    row = {"passage": builder.passages[0], "score": 1.0, "routes": ["bm25"]}
    builder._retrieve = lambda *args: [row]
    request = builder.build_request(batch, model_id="fake", prompt_version="v1")
    payload = json.loads(request.messages[1]["content"].split("REQUEST_JSON:", 1)[1])
    assert len(payload["rag_evidence_pool"]) == 1
    assert ("For route-aware evidence" in request.messages[0]["content"]) == (index_prefix == "tctx_v3_")
    assert (
        payload["units"][0]["rag_evidence_ids"]
        == payload["units"][1]["rag_evidence_ids"]
    )


def test_vector_and_rerank_cache_survive_a_new_cache_instance(tmp_path):
    root, _ = corpus_fixture(tmp_path)
    path = tmp_path / "cache.sqlite3"
    e, r = FakeEmbedder(), FakeReranker()
    first, requests, _ = prepare(
        root,
        {"chunk_tokens": 16},
        batch_size=1,
        embedder=e,
        reranker=r,
        cache=RetrievalCache(path),
    )
    assert e.calls
    e2, r2 = FakeEmbedder(), FakeReranker()
    again, repeated, _ = prepare(
        root,
        {"chunk_tokens": 16},
        batch_size=1,
        embedder=e2,
        reranker=r2,
        cache=RetrievalCache(path),
    )
    assert not e2.calls and not r2.calls
    assert first.plan_id == again.plan_id
    assert [x.request_id for x in requests] == [x.request_id for x in repeated]
    changed = FakeEmbedder()
    changed.identity = "new-model"
    altered, _, _ = prepare(
        root, {"chunk_tokens": 16}, embedder=changed, cache=RetrievalCache(path)
    )
    assert changed.calls and altered.plan_id != first.plan_id


def test_cache_rejects_corruption_and_invalid_vectors(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = RetrievalCache(path)
    cache.put("key", [1.0, 0.0])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE retrieval SET value='[0,1]' WHERE key='key'")
    with pytest.raises(ValueError, match="checksum"):
        RetrievalCache(path).get("key")

    class Invalid(FakeEmbedder):
        def embed(self, texts, **kwargs):
            return [[float("nan")]] * len(texts)

    with pytest.raises(ValueError, match="invalid"):
        RetrievalCache().embeddings(Invalid(), ["source"])


def test_changed_content_only_embeds_missing_vectors(tmp_path):
    cache = RetrievalCache(tmp_path / "cache.sqlite3")
    e = FakeEmbedder()
    cache.embeddings(e, ["silver key", "rocket"])
    e.calls.clear()
    cache.embeddings(e, ["silver key", "train"])
    assert e.calls == [(False, ["train"])]


@pytest.mark.parametrize("neural", [False, True])
def test_context_preparation_can_stop_before_translation(tmp_path, neural):
    root, _ = corpus_fixture(tmp_path)
    messages = []

    def stop(message):
        messages.append(message)
        raise InterruptedError("user cancelled")

    with pytest.raises(InterruptedError, match="user cancelled"):
        prepare(
            root,
            embedder=FakeEmbedder() if neural else None,
            progress=stop,
        )
    assert len(messages) == 1
    assert ("本地语义索引" if neural else "准备翻译上下文") in messages[0]


def test_web_context_cancellation_never_calls_translation_model(tmp_path, monkeypatch):
    from nagi import webapp

    root, _ = corpus_fixture(tmp_path)
    job = webapp.TranslationJob("cancel", str(tmp_path), "pilot", str(tmp_path / "out"))
    job.model_config = {"model": "fake"}
    job.retrieval_models = (FakeEmbedder(), FakeReranker(), ())
    job.cancel_event.set()
    monkeypatch.setattr(webapp, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        webapp,
        "assess_game_directory",
        lambda _: {
            "status": "supported",
            "key_available": True,
            "supported_archive_count": 1,
        },
    )
    monkeypatch.setattr(
        webapp, "_prepare_source", lambda *a: (tmp_path / "export", root)
    )
    monkeypatch.setattr(
        webapp,
        "_model_client",
        lambda: pytest.fail("cancelled job called translation API"),
    )
    webapp._run_job(job)
    assert job.status == "cancelled"
    assert not (tmp_path / "out/translation-run").exists()


def test_resume_identity_and_policy_changes(tmp_path):
    root, _ = corpus_fixture(tmp_path)
    plan, requests, _ = prepare(root, limit=1)
    spec = build_translation_run_spec(plan, requests)
    initialize_translation_run(spec, tmp_path / "run")
    again, repeated, _ = prepare(root, limit=1)
    assert (
        load_translation_checkpoint(
            build_translation_run_spec(again, repeated), tmp_path / "run"
        )["revision"]
        == 0
    )
    changed, _, _ = prepare(root, {"rag_budget_chars": 0}, limit=1)
    assert changed.plan_id != plan.plan_id


@pytest.mark.parametrize(
    "config",
    [
        {"translation_memory": []},
        {"terminology": []},
        {"script_routes": {}},
        {"rag_budget_chars": -1},
        {"rag_budget_chars": True},
        {"chunk_tokens": 10000},
        {"overlap_turns": 3},
        {"semantic_min_score": float("nan")},
    ],
)
def test_reference_imports_and_invalid_policies_are_rejected(config):
    with pytest.raises(ValueError):
        TranslationContextConfig.from_dict(config)


def test_cli_context_preview_remains_offline_and_read_only(tmp_path, capsys):
    from nagi import cli

    root, _ = corpus_fixture(tmp_path)
    before = {p: p.read_bytes() for p in root.iterdir()}
    assert (
        cli.main(
            [
                "translate",
                "preview-request",
                str(root),
                "--with-context",
                "--limit",
                "1",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["rag_index_id"].startswith("tctx_v3_")
    assert not payload["units"][0][
        "rag_evidence"
    ]  # No future evidence for the first line.
    assert before == {p: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(SystemExit):
        cli.main(
            ["translate", "preview-request", str(root), "--context-file", "unused.json"]
        )


def test_web_uses_automatic_context_and_preserves_original_game(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nagi import webapp

    root, _ = corpus_fixture(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    (game / "untouched.pack").write_bytes(b"original")
    calls = []

    def complete(messages, max_new_tokens):
        if not messages[1]["content"].startswith("REQUEST_JSON:"):
            names = json.loads(messages[1]["content"])["characters"]
            return json.dumps({"translations": [{"id": n["id"], "text": n["source_name"]} for n in names]})
        calls.append(messages)
        payload = json.loads(messages[1]["content"].split("REQUEST_JSON:", 1)[1])
        return json.dumps(
            {
                "schema_version": 1,
                "request_id": payload["request_id"],
                "translations": [
                    {
                        "unit_id": u["unit_id"],
                        "segment_id": u["segment_id"],
                        "translated_text": "译文",
                    }
                    for u in payload["units"]
                ],
            }
        )

    monkeypatch.setattr(webapp, "_configured_model", lambda: {"model": "fake"})
    monkeypatch.setattr(
        webapp, "_model_client", lambda: SimpleNamespace(complete=complete)
    )
    monkeypatch.setattr(webapp, "_completed_translation_run", lambda spec: None)
    monkeypatch.setattr(webapp, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        webapp, "local_models", lambda: (FakeEmbedder(), FakeReranker(), ())
    )
    monkeypatch.setattr(
        webapp,
        "build_translation_patch_preview",
        lambda *a, **k: SimpleNamespace(patch_eligible=True),
    )
    monkeypatch.setattr(webapp, "publish_translation_preview", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "apply_translation_preview", lambda *a, **k: None)
    job = webapp.TranslationJob("automatic", str(game), "pilot", str(tmp_path / "out"))
    webapp._translate_and_publish(job, tmp_path / "export", root)
    assert (
        job.status == "completed" and job.context_summary["retrieval_mode"] == "hybrid"
    )
    saved = json.loads(
        (tmp_path / "out/translation-run/context-config.json").read_text()
    )
    assert saved == TranslationContextConfig().to_dict()
    assert "translation_memory" not in saved
    assert (game / "untouched.pack").read_bytes() == b"original"
    count = len(calls)
    webapp._translate_and_publish(job, tmp_path / "export", root)
    assert len(calls) == count
