"""Validated Segment v1 ingestion and deterministic keyword tokenization."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..gameio.segments import (
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_SEGMENTS,
    SegmentContractError,
    TextSegment,
)
from .models import KeywordDocument, TOKENIZER_VERSION


MAX_CORPUS_BYTES = 1024 * 1024 * 1024
MAX_CORPUS_REPORT_BYTES = 32 * 1024 * 1024


def _is_cjk(char):
    codepoint = ord(char)
    return (
        0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def normalize_keyword_text(text):
    if not isinstance(text, str):
        raise TypeError("keyword text must be a string")
    return unicodedata.normalize("NFKC", text).casefold()


def tokenize_keyword_text(text):
    """Return stable word plus CJK unigram/bigram tokens."""

    normalized = normalize_keyword_text(text)
    tokens = []
    cursor = 0
    while cursor < len(normalized):
        char = normalized[cursor]
        if _is_cjk(char):
            end = cursor + 1
            while end < len(normalized) and _is_cjk(normalized[end]):
                end += 1
            run = normalized[cursor:end]
            tokens.extend(f"c:{value}" for value in run)
            tokens.extend(f"b:{run[index:index + 2]}" for index in range(len(run) - 1))
            cursor = end
            continue
        if char.isalnum() or char == "_":
            end = cursor + 1
            while end < len(normalized):
                following = normalized[end]
                if _is_cjk(following) or not (following.isalnum() or following == "_"):
                    break
                end += 1
            tokens.append("w:" + normalized[cursor:end])
            cursor = end
            continue
        cursor += 1
    return tuple(tokens)


@dataclass(frozen=True)
class IngestedKeywordCorpus:
    source_dir: str
    segments_path: str
    source_sha256: str
    source_size_bytes: int
    segment_count: int
    translatable_segment_count: int
    kind_counts: tuple[tuple[str, int], ...]
    documents: tuple[KeywordDocument, ...]
    postings: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    posting_count: int
    tokenizer_version: int = TOKENIZER_VERSION


def _resolve_corpus_source(corpus_path):
    source = Path(corpus_path).expanduser().resolve()
    if source.is_dir():
        segments_path = source / "segments.jsonl"
        report_path = source / "parse-report.json"
    else:
        segments_path = source
        source = source.parent
        report_path = source / "parse-report.json"
    if segments_path.is_symlink() or not segments_path.is_file():
        raise ValueError("segments.jsonl is missing or not a regular file")
    if segments_path.stat().st_size > MAX_CORPUS_BYTES:
        raise ValueError("segments.jsonl exceeds the ingestion size limit")
    expected_hash = None
    if report_path.is_file() and not report_path.is_symlink():
        if report_path.stat().st_size > MAX_CORPUS_REPORT_BYTES:
            raise ValueError("parse-report.json exceeds the ingestion size limit")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            expected_hash = report["artifacts"]["segments"]["sha256"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            raise ValueError("parse-report.json does not contain a valid segments hash")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("parse-report.json contains an invalid segments hash")
        expected_hash = expected_hash.casefold()
    return source, segments_path, expected_hash


def ingest_segment_corpus(corpus_path, *, include_nontranslatable=False):
    """Validate one Segment v1 JSONL corpus and build an in-memory keyword index."""

    source, segments_path, expected_hash = _resolve_corpus_source(corpus_path)
    digest = hashlib.sha256()
    ids = {}
    documents = []
    postings = defaultdict(list)
    kind_counts = Counter()
    segment_count = 0
    translatable_count = 0
    with segments_path.open("rb") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if len(raw) > MAX_JSONL_LINE_BYTES:
                raise ValueError(f"Segment v1 line {line_number} exceeds the protocol limit")
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise ValueError("segments.jsonl must end every record with LF")
            try:
                payload = json.loads(raw)
                segment = TextSegment.from_dict(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, SegmentContractError, ValueError) as exc:
                raise ValueError(f"invalid Segment v1 record at line {line_number}: {exc}")
            segment_count += 1
            if segment_count > MAX_JSONL_SEGMENTS:
                raise ValueError("segments.jsonl exceeds the Segment v1 count limit")
            if segment.segment_id in ids:
                raise ValueError("segments.jsonl contains duplicate segment IDs")
            ids[segment.segment_id] = (
                segment.previous_segment_id,
                segment.next_segment_id,
            )
            kind_counts[segment.kind] += 1
            translatable_count += segment.translatable
            if not include_nontranslatable and not segment.translatable:
                continue
            tokens = tokenize_keyword_text(segment.normalized_text)
            token_counts = Counter(tokens)
            doc_id = len(documents)
            documents.append(
                KeywordDocument.from_segment(segment=segment, doc_id=doc_id, token_count=len(tokens))
            )
            for token, frequency in sorted(token_counts.items()):
                postings[token].append((doc_id, frequency))
    if not segment_count:
        raise ValueError("segments.jsonl contains no Segment v1 records")
    for segment_id, (previous, following) in ids.items():
        if previous is not None and (previous not in ids or ids[previous][1] != segment_id):
            raise ValueError("segments.jsonl contains a missing or non-reciprocal previous link")
        if following is not None and (following not in ids or ids[following][0] != segment_id):
            raise ValueError("segments.jsonl contains a missing or non-reciprocal next link")
    source_hash = digest.hexdigest()
    if expected_hash is not None and source_hash != expected_hash:
        raise ValueError("segments.jsonl hash does not match parse-report.json")
    stable_postings = tuple(
        (token, tuple(values)) for token, values in sorted(postings.items())
    )
    return IngestedKeywordCorpus(
        source_dir=str(source),
        segments_path=str(segments_path),
        source_sha256=source_hash,
        source_size_bytes=segments_path.stat().st_size,
        segment_count=segment_count,
        translatable_segment_count=translatable_count,
        kind_counts=tuple(sorted(kind_counts.items())),
        documents=tuple(documents),
        postings=stable_postings,
        posting_count=sum(len(values) for values in postings.values()),
    )
