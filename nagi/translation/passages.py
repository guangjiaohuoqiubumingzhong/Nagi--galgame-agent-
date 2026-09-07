"""Scene-local passages and conservative, source-derived narrative boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..gameio.segments import TextSegment
from ..rag.ingest import normalize_keyword_text, tokenize_keyword_text
from .models import _canonical_sha256
from .routes import analyze_corpus_routes

PASSAGE_VERSION = "scene-passages-v2"
TEXT_KINDS = frozenset({"dialogue", "narration"})
# Unknown instructions are barriers. Never assume an assignment/jump is visual.
PRESENTATION_CONTROL = re.compile(
    r"^\^(?:bgm|bgmstop|se|sestop|voice|wait|fade|bg|cg|chara|clear|messageclear)(?:[,\s]|$)",
    re.IGNORECASE,
)
ENTITY_SUFFIX = re.compile(
    r"[一-龯ァ-ヶーA-Za-z0-9]{2,20}(?:学園|学院|大学|高校|学校|喫茶店|カフェ|駅|公園|病院|図書館)"
)
ENGLISH_NAME = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}\b")
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "to",
        "of",
        "and",
        "or",
        "in",
        "on",
        "at",
        "for",
        "this",
        "that",
        "do",
        "not",
    ]
)


def keywords(text):
    return {
        word
        for word in tokenize_keyword_text(text)
        if word.startswith("b:")
        or (word.startswith("w:") and len(word[2:]) > 1 and word[2:] not in STOP_WORDS)
    }


def lexical_relevant(query, candidate):
    shared = query & candidate
    strong = any(word.startswith("w:") and len(word[2:]) >= 4 for word in shared)
    return (
        bool(shared)
        and (len(shared) >= 2 or strong)
        and len(shared) / max(1, len(query)) >= 0.2
    )


def entities(text, speakers=()):
    # Only literal occurrences; no guessed aliases, entity merges or translations.
    value = normalize_keyword_text(text)
    found = {
        normalize_keyword_text(name)
        for name in speakers
        if name and normalize_keyword_text(name) in value
    }
    found.update(
        normalize_keyword_text(match.group())
        for regex in (ENTITY_SUFFIX, ENGLISH_NAME)
        for match in regex.finditer(text)
    )
    return frozenset(found)


def display_text(unit):
    return f"{unit.speaker}: {unit.source_text}" if unit.speaker else unit.source_text


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    scope: tuple
    scene: str | None
    units: tuple
    text: str
    token_count: int

    @property
    def segment_ids(self):
        return tuple(unit.segment_id for unit in self.units)


class NarrativeCorpus:
    def __init__(self, plan, *, entry_segment_ids=()):
        self.units = {
            unit.segment_id: unit for batch in plan.batches for unit in batch.units
        }
        self.groups = {}
        self.positions = {}
        self.sequences = defaultdict(list)
        files = defaultdict(list)
        digest = hashlib.sha256()
        with open(plan.segments_path, "rb") as stream:
            for raw in stream:
                digest.update(raw)
                segment = TextSegment.from_dict(json.loads(raw))
                files[(segment.source.archive_name, segment.source.output_path)].append(
                    segment
                )
        if digest.hexdigest() != plan.segments_sha256:
            raise ValueError("corpus changed while constructing narrative boundaries")
        self.routes = analyze_corpus_routes(plan, entry_segment_ids=entry_segment_ids)
        for file_key, segments in files.items():
            # This is source order ONLY, not an inferred game-wide route order.
            segments.sort(key=lambda s: (s.source.line_start, s.source.byte_start))
            block, previous_scene = 0, None
            for segment in segments:
                if segment.scene != previous_scene:
                    block += 1
                    previous_scene = segment.scene
                barrier = segment.kind in {"label", "choice", "unknown"} or (
                    segment.kind == "control"
                    and not PRESENTATION_CONTROL.match(segment.source_text.strip())
                )
                if barrier:
                    block += 1
                if segment.segment_id in self.units:
                    scope = (*file_key, block)
                    self.groups[segment.segment_id] = scope
                    if segment.kind in TEXT_KINDS:
                        self.positions[segment.segment_id] = len(self.sequences[scope])
                        self.sequences[scope].append(self.units[segment.segment_id])
                if barrier:
                    block += 1
        self.speakers = tuple(
            sorted({u.speaker for u in self.units.values() if u.speaker})
        )

    def previous(self, unit, limit=4):
        sequence = self.sequences.get(self.groups.get(unit.segment_id), ())
        position = self.positions.get(unit.segment_id)
        previous = (
            tuple(sequence[max(0, position - limit) : position])
            if position is not None
            else ()
        )
        if self.routes:
            previous = tuple(u for u in previous if self.routes.classify(u.segment_id, unit.segment_id) == "must")
        return previous

    def adjacent(self, unit):
        sequence = self.sequences.get(self.groups.get(unit.segment_id), ())
        position = self.positions.get(unit.segment_id)
        if position is None:
            return None, None
        before = sequence[position - 1] if position else None
        after = sequence[position + 1] if position + 1 < len(sequence) else None
        if self.routes:
            if before and self.routes.classify(before.segment_id, unit.segment_id) != "must":
                before = None
            if after and self.routes.classify(unit.segment_id, after.segment_id) != "must":
                after = None
        return before, after

    def passages(self, count_tokens, *, target_tokens=320, overlap_turns=2):
        result = []
        for scope, sequence in self.sequences.items():
            offset = 0
            while offset < len(sequence):
                selected = []
                end = offset
                while end < len(sequence):
                    proposed = [*selected, sequence[end]]
                    text = "\n".join(display_text(unit) for unit in proposed)
                    if count_tokens(text) > target_tokens:
                        break
                    selected = proposed
                    end += 1
                if not selected:
                    # Preserve the translation unit; omit oversized retrieval evidence.
                    offset += 1
                    continue
                text = "\n".join(display_text(unit) for unit in selected)
                tokens = count_tokens(text)
                identity = {
                    "version": PASSAGE_VERSION,
                    "scope": scope,
                    "target": target_tokens,
                    "overlap": overlap_turns,
                    "segments": [unit.segment_id for unit in selected],
                    "text": text,
                }
                result.append(
                    Passage(
                        "chunk_" + _canonical_sha256(identity),
                        scope,
                        selected[0].scene,
                        tuple(selected),
                        text,
                        tokens,
                    )
                )
                if end == len(sequence):
                    break
                overlap = 0
                for size in range(1, min(overlap_turns, len(selected) - 1) + 1):
                    tail = "\n".join(display_text(unit) for unit in selected[-size:])
                    if count_tokens(tail) <= max(1, int(tokens * 0.2)):
                        overlap = size
                offset = end - overlap
        return tuple(result)

    def eligible(self, passage, unit, excluded_ids):
        if self.routes:
            if unit.segment_id not in self.routes.bits:
                return False
            _, must = self.routes.history_masks(unit.segment_id)
            return (
                not set(passage.segment_ids) & excluded_ids
                and all(self.routes.bits.get(sid, 0) & must for sid in passage.segment_ids)
            )
        position = self.positions.get(unit.segment_id)
        return (
            position is not None
            and passage.scope == self.groups[unit.segment_id]
            and self.positions[passage.units[-1].segment_id] < position
            and not set(passage.segment_ids) & excluded_ids
        )


def bm25_candidates(passages, query, speakers, limit=6):
    """BM25 inside the prefiltered narrative scope, with literal-entity boosting."""
    tokens = keywords(query)
    if not tokens or not passages:
        return []
    rows = [(p, Counter(tokenize_keyword_text(p.text))) for p in passages]
    frequency = Counter(token for _, counts in rows for token in counts)
    mean_length = sum(sum(counts.values()) for _, counts in rows) / len(rows) or 1
    query_entities = entities(query, speakers)
    ranked = []
    import math

    for passage, counts in rows:
        if not lexical_relevant(tokens, keywords(passage.text)):
            continue
        length = sum(counts.values())
        score = 0.0
        for token in tokens:
            tf = counts.get(token, 0)
            if tf:
                idf = math.log(
                    1 + (len(rows) - frequency[token] + 0.5) / (frequency[token] + 0.5)
                )
                score += (
                    idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / mean_length))
                )
        score *= 1 + 0.5 * len(query_entities & entities(passage.text, speakers))
        ranked.append((passage, score))
    return sorted(ranked, key=lambda row: (-row[1], row[0].chunk_id))[:limit]
