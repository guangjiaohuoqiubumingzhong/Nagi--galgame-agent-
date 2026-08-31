import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nagi.gameio.segments import (
    SEGMENT_KINDS,
    SEGMENT_SCHEMA_VERSION,
    SegmentContractError,
    SegmentInlineToken,
    SegmentSource,
    TextSegment,
    build_text_segment,
    link_segment_sequence,
    normalize_segment_text,
    segments_from_jsonl,
    segments_to_jsonl,
)


FIXTURE_PATH = Path("tests/fixtures/qlie_synthetic/segments-v1.jsonl")
SCHEMA_PATH = Path("docs/product/qlie-translation/segments-v1.schema.json")
SOURCE_HASH = hashlib.sha256(b"synthetic qlie fixture source v1").hexdigest()


def make_source(**overrides):
    values = {
        "engine": "qlie",
        "archive_name": "GameData/data6.pack",
        "internal_path": "scenario\\fixture.s",
        "output_path": "layers/GameData/data6.pack/scenario/fixture.s",
        "entry_index": 42,
        "conflict_group": "scenario/fixture.s",
        "source_sha256": SOURCE_HASH,
        "source_size_bytes": 512,
        "encoding": "utf-16-le-bom",
        "byte_start": 32,
        "byte_end": 60,
        "line_start": 3,
        "line_end": 3,
    }
    values.update(overrides)
    return SegmentSource(**values)


def make_golden_segments():
    dialogue = build_text_segment(
        kind="dialogue",
        source=make_source(),
        source_text="Hello {name}!",
        translatable=True,
        speaker="Alice",
        scene="intro",
        placeholders=(SegmentInlineToken("variable", "{name}", 6, 12),),
    )
    choice = build_text_segment(
        kind="choice",
        source=make_source(byte_start=64, byte_end=90, line_start=4, line_end=4),
        source_text="[wait]Continue?",
        translatable=True,
        scene="intro",
        tags=(SegmentInlineToken("control", "[wait]", 0, 6),),
    )
    return link_segment_sequence((dialogue, choice))


def test_segment_v1_matches_golden_jsonl_and_round_trips():
    segments = make_golden_segments()
    golden = FIXTURE_PATH.read_text(encoding="utf-8")

    serialized = segments_to_jsonl(segments)
    restored = segments_from_jsonl(serialized)

    assert serialized == golden
    assert restored == segments
    assert segments_to_jsonl(restored) == golden
    assert segments[0].next_segment_id == segments[1].segment_id
    assert segments[1].previous_segment_id == segments[0].segment_id


def test_segment_id_is_relocation_stable_but_content_sensitive():
    source = make_source()
    original = build_text_segment(
        kind="dialogue",
        source=source,
        source_text="Hello",
        translatable=True,
    )
    relocated = build_text_segment(
        kind="narration",
        source=replace(
            source,
            output_path="resolved/moved/fixture.s",
            internal_path="scenario/fixture.s",
            line_start=9,
            line_end=9,
        ),
        source_text="Hello",
        translatable=True,
    )
    moved_span = build_text_segment(
        kind="dialogue",
        source=replace(source, byte_start=34, byte_end=62),
        source_text="Hello",
        translatable=True,
    )
    changed_text = build_text_segment(
        kind="dialogue",
        source=source,
        source_text="Hello!",
        translatable=True,
    )
    changed_source = build_text_segment(
        kind="dialogue",
        source=replace(source, source_sha256="0" * 64),
        source_text="Hello",
        translatable=True,
    )

    assert relocated.segment_id == original.segment_id
    assert moved_span.segment_id != original.segment_id
    assert changed_text.segment_id != original.segment_id
    assert changed_source.segment_id != original.segment_id
    assert "D:" not in original.segment_id


def test_normalization_is_unicode_and_newline_only():
    source_text = "Cafe\u0301\r\n  value  \r"

    normalized = normalize_segment_text(source_text)

    assert normalized == "Café\n  value  \n"


def test_contract_rejects_unsafe_source_and_invalid_spans():
    with pytest.raises(SegmentContractError, match="safe relative path"):
        make_source(output_path="../outside.s")
    with pytest.raises(SegmentContractError, match="safe relative path"):
        make_source(internal_path="C:\\game\\main.s")
    with pytest.raises(SegmentContractError, match="exceeds"):
        make_source(byte_end=513)
    with pytest.raises(SegmentContractError, match="lowercase SHA-256"):
        make_source(source_sha256="ABC")
    with pytest.raises(SegmentContractError, match="integers"):
        make_source(byte_start=False)


def test_contract_rejects_token_mismatch_and_unknown_json_fields():
    with pytest.raises(SegmentContractError, match="does not match"):
        build_text_segment(
            kind="dialogue",
            source=make_source(),
            source_text="Hello {name}!",
            translatable=True,
            placeholders=(SegmentInlineToken("variable", "{wrong}", 6, 13),),
        )

    payload = make_golden_segments()[0].to_dict()
    payload["unexpected"] = True
    with pytest.raises(SegmentContractError, match="unknown fields"):
        TextSegment.from_dict(payload)


def test_jsonl_rejects_dangling_or_nonreciprocal_links():
    first, second = make_golden_segments()
    broken = (replace(first, next_segment_id=None), second)

    with pytest.raises(SegmentContractError, match="reciprocal"):
        segments_to_jsonl(broken)

    payload = first.to_dict()
    payload["next_segment_id"] = "seg_v1_" + "f" * 64
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with pytest.raises(SegmentContractError, match="missing or not reciprocal"):
        segments_from_jsonl(line + "\n")


def test_schema_artifact_tracks_runtime_contract():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == SEGMENT_SCHEMA_VERSION
    assert set(schema["properties"]["kind"]["enum"]) == SEGMENT_KINDS
    assert set(schema["required"]) == set(make_golden_segments()[0].to_dict())
    assert schema["additionalProperties"] is False
