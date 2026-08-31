"""Explicit local-model smoke tests: no network, chat API or real game input."""

import json
import os

import pytest
from test_translation_context import corpus_fixture

from nagi.rag.semantic import RetrievalCache, local_models
from nagi.translation.context import prepare_translation_requests

pytestmark = pytest.mark.skipif(
    os.environ.get("NAGI_TEST_LOCAL_RAG") != "1",
    reason="explicit local model smoke test",
)


def test_japanese_cafe_event_is_ranked_above_unrelated_passages():
    embedding, reranker, missing = local_models()
    assert not missing
    query = "先週約束した喫茶店に、放課後一緒に行こう。"
    passages = [
        "土曜日の授業が終わったら、駅前のカフェで二人でお茶を飲もうと約束した。",
        "今日は図書館で数学の試験勉強をした。",
        "地球から月までの距離は約三十八万キロメートルです。",
    ]
    vectors = embedding.embed(passages)
    query_vector = embedding.embed([query], query=True)[0]
    cosine = [sum(a * b for a, b in zip(query_vector, v)) for v in vectors]
    scores = reranker.rerank(query, passages)
    assert len(vectors[0]) == 384
    assert cosine[0] > max(cosine[1:])
    assert scores[0] >= -3 and max(scores[1:]) < -3


def test_actual_models_feed_bounded_historical_evidence_into_messages(tmp_path):
    embedding, reranker, missing = local_models()
    assert not missing
    rows = [
        {
            "text": "土曜日の授業が終わったら、駅前のカフェで二人でお茶を飲もうと約束した。"
        },
        *[
            {"text": "図書館では学生たちが静かに数学の試験勉強を続けていた。"}
            for _ in range(12)
        ],
        {"text": "先週約束した喫茶店に、放課後一緒に行こう。", "speaker": "Target"},
        {"text": "その約束は別の世界では実現しなかった。", "path": "other.s"},
    ]
    root, segments = corpus_fixture(tmp_path, rows)
    summary = {}
    _, requests, _ = prepare_translation_requests(
        root,
        model_id="no-chat-model",
        speaker="Target",
        config={"chunk_tokens": 40, "query_tokens": 32},
        embedder=embedding,
        reranker=reranker,
        cache=RetrievalCache(tmp_path / "vectors.sqlite3"),
        summary=summary,
    )
    payload = json.loads(
        requests[0].messages[1]["content"].split("REQUEST_JSON:", 1)[1]
    )
    pool = payload["rag_evidence_pool"]
    assert pool
    ids = [
        sid
        for item in pool.values()
        for sid in json.loads(item["text"])["passage_segment_ids"]
    ]
    assert segments[0].segment_id in ids
    assert segments[-1].segment_id not in ids
    assert summary["retrieval_mode"] == "hybrid"
