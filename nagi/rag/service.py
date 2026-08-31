"""Small presentation layer for local keyword retrieval."""

from __future__ import annotations

from .index import load_keyword_index
from .models import KeywordQuery
from .retriever import search_keyword_index


def query_keyword_index(index_dir, text, *, source_corpus=None, **filters):
    index = load_keyword_index(index_dir, source_corpus=source_corpus)
    query = KeywordQuery(text=text, **filters)
    return search_keyword_index(index, query)


def render_search_response_text(response):
    lines = [
        f"Keyword search: {len(response.results)} results in {response.elapsed_ms:.3f} ms",
        f"index_id: {response.index_id}",
    ]
    for result in response.results:
        document = result.document
        source = f"{document.archive_name}:{document.output_path}:{document.byte_start}"
        speaker = f" speaker={document.speaker}" if document.speaker else ""
        scene = f" scene={document.scene}" if document.scene else ""
        lines.append(
            f"{result.rank}. score={result.score:.6f} kind={document.kind}{speaker}{scene}"
        )
        lines.append(f"   source={source} segment_id={document.segment_id}")
        lines.append(f"   text={document.normalized_text}")
        lines.append(f"   matched={', '.join(result.matched_tokens)}")
    return "\n".join(lines)
