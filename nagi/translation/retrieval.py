"""Shared BM25 + local E5 retrieval over engine-validated history candidates."""

from .passages import bm25_candidates, keywords, lexical_relevant


def retrieve_history(query, candidates, *, config, embedder, reranker, cache,
                     vectors, speakers=()):
    """Rank an already scoped candidate pool; never expand across route boundaries."""
    lexical = bm25_candidates(
        candidates, query, speakers, config.bm25_k
    )
    vector = []
    if embedder:
        embedding = cache.embeddings(embedder, [query], query=True)[0]
        for passage in candidates:
            stored = vectors.get(passage.chunk_id)
            if stored is None:
                continue
            if len(embedding) != len(stored):
                raise ValueError("query and passage embedding dimensions differ")
            score = sum(a * b for a, b in zip(embedding, stored))
            if score >= config.semantic_min_score:
                vector.append((passage, score))
        vector.sort(key=lambda row: (-row[1], row[0].chunk_id))
        vector = vector[: config.vector_k]
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
    )[: config.rerank_k]
    if reranker and ranked:
        ranked = [
            row
            for row in ranked
            if reranker.fits_pair(query, row["passage"].text)
        ]
        scores = cache.scores(
            reranker, query, [r["passage"].text for r in ranked]
        )
        for row, score in zip(ranked, scores):
            row["score"] = score
        ranked = [
            row for row in ranked if row["score"] >= config.rerank_min_score
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
    return ranked[: config.per_query_k]
