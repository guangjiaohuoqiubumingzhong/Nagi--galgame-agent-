import hashlib
import json
from dataclasses import replace

from nagi.gameio.qlie.corpus import (
    apply_qlie_corpus_plan,
    build_qlie_corpus_plan,
    write_unknown_review_template,
)


def write_export_workspace(root, scripts):
    root.mkdir()
    items = []
    for index, script in enumerate(scripts):
        output_path = script["output_path"]
        data = script["data"]
        target = root.joinpath(*output_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        items.append(
            {
                "archive_name": script.get("archive_name", "GameData/data6.pack"),
                "archive_path": "synthetic/data6.pack",
                "archive_rank": 0,
                "compression_flag": 1,
                "conflict_group": script.get("conflict_group"),
                "decoded_sha256": hashlib.sha256(data).hexdigest(),
                "decoded_size": len(data),
                "entry_hash": index,
                "entry_hash_hex": f"{index:08x}",
                "entry_index": index,
                "internal_path": script.get("internal_path", f"scenario\\{index}.s"),
                "obfuscation_flag": 2,
                "original_size": len(data),
                "output_path": output_path,
                "reason": "synthetic fixture",
                "shadowed_by": None,
                "status": "exported",
                "stored_sha256": hashlib.sha256(data).hexdigest(),
                "stored_size": len(data),
            }
        )
    manifest = {
        "schema_version": 1,
        "engine": "qlie",
        "extensions": [".s"],
        "items": items,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    (root / "manifest.json").write_bytes(manifest_bytes)
    return manifest_bytes


def complete_review(template_path, *, label="not_translatable"):
    items = [json.loads(line) for line in template_path.read_text(encoding="utf-8").splitlines()]
    for item in items:
        item["label"] = label
    with template_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            "\n".join(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                for item in items
            )
            + "\n"
        )


def test_corpus_plan_is_content_free_and_requires_unknown_review(tmp_path):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    secret = "NeverEmitUnknownText"
    write_export_workspace(
        source,
        [
            {
                "output_path": "layers/data8.pack/scenario/main.s",
                "internal_path": "scenario\\main.s",
                "archive_name": "GameData/data8.pack",
                "conflict_group": "scenario/main.s",
                "data": ("@@scene\r\n「Dialogue」\r\n" + secret + "\r\n").encode("utf-8"),
            }
        ],
    )

    plan = build_qlie_corpus_plan(source, output)
    payload = plan.to_dict()
    serialized = plan.to_json()

    assert plan.status == "review_required"
    assert payload["summary"] == {
        "conflict_group_count": 1,
        "conflict_variant_count": 1,
        "file_count": 1,
        "kind_counts": {"dialogue": 1, "label": 1, "unknown": 1},
        "layer_file_count": 1,
        "line_count": 3,
        "nonblank_line_count": 3,
        "ready_file_count": 1,
        "segment_count": 3,
        "source_bytes": len(("@@scene\r\n「Dialogue」\r\n" + secret + "\r\n").encode("utf-8")),
        "translatable_count": 1,
        "unknown_count": 1,
    }
    assert payload["unknown_review"]["shape_counts"] == {"A": 1}
    assert secret not in serialized
    assert not output.exists()


def test_complete_review_enables_transactional_publish_and_is_deterministic(tmp_path):
    source = tmp_path / "export"
    output_one = tmp_path / "corpus-one"
    output_two = tmp_path / "corpus-two"
    review = tmp_path / "unknown-review.jsonl"
    manifest_bytes = write_export_workspace(
        source,
        [
            {
                "output_path": "resolved/scenario/main.s",
                "internal_path": "scenario\\main.s",
                "data": "@@scene\n「Synthetic dialogue」\nplain data\n".encode("utf-8"),
            }
        ],
    )
    initial = build_qlie_corpus_plan(source, output_one)
    write_unknown_review_template(initial, review)
    complete_review(review)

    first_plan = build_qlie_corpus_plan(source, output_one, review_path=review)
    first_result = apply_qlie_corpus_plan(first_plan)
    second_plan = build_qlie_corpus_plan(source, output_two, review_path=review)
    second_result = apply_qlie_corpus_plan(second_plan)

    assert first_plan.status == "ready"
    assert first_plan.unknown_review.status == "accepted"
    assert first_plan.unknown_review.recall == 1.0
    assert first_result.status == "published"
    assert second_result.status == "published"
    assert (output_one / "segments.jsonl").read_bytes() == (
        output_two / "segments.jsonl"
    ).read_bytes()
    assert (output_one / "source-manifest.json").read_bytes() == manifest_bytes
    assert (output_one / "source-manifest.json").read_bytes() == (
        output_two / "source-manifest.json"
    ).read_bytes()
    report = json.loads((output_one / "parse-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "published"
    assert report["unknown_review"]["reviewed_count"] == 1
    assert report["artifacts"]["segments"]["sha256"] == first_result.segments_sha256
    assert not any(path.name.startswith(".corpus-one.staging-") for path in tmp_path.iterdir())


def test_reviewed_missed_text_can_block_recall_target(tmp_path):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    review = tmp_path / "unknown-review.jsonl"
    write_export_workspace(
        source,
        [{"output_path": "resolved/main.s", "data": b"plain unknown\n"}],
    )
    initial = build_qlie_corpus_plan(source, output)
    write_unknown_review_template(initial, review)
    complete_review(review, label="translatable")

    plan = build_qlie_corpus_plan(source, output, review_path=review)
    result = apply_qlie_corpus_plan(plan)

    assert plan.status == "recall_below_target"
    assert plan.unknown_review.recall == 0.0
    assert result.status == "blocked"
    assert not output.exists()


def test_incomplete_unknown_review_keeps_publish_blocked(tmp_path):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    review = tmp_path / "unknown-review.jsonl"
    write_export_workspace(
        source,
        [{"output_path": "resolved/main.s", "data": b"unknown one\nunknown two\n"}],
    )
    initial = build_qlie_corpus_plan(source, output)
    write_unknown_review_template(initial, review)
    first_line = review.read_text(encoding="utf-8").splitlines()[0]
    item = json.loads(first_line)
    item["label"] = "not_translatable"
    review.write_text(json.dumps(item, sort_keys=True) + "\n", encoding="utf-8")

    plan = build_qlie_corpus_plan(source, output, review_path=review)

    assert plan.status == "review_required"
    assert plan.unknown_review.status == "invalid"
    assert "incomplete" in plan.unknown_review.reason
    assert not output.exists()


def test_apply_rejects_source_change_after_plan_without_output(tmp_path):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    write_export_workspace(
        source,
        [{"output_path": "resolved/main.s", "data": "「fixture」\n".encode("utf-8")}],
    )
    plan = build_qlie_corpus_plan(source, output)
    (source / "resolved" / "main.s").write_bytes("「changed」\n".encode("utf-8"))

    result = apply_qlie_corpus_plan(plan)

    assert plan.status == "ready"
    assert result.status == "source_changed"
    assert not output.exists()


def test_apply_rejects_broken_reciprocal_links_without_output(tmp_path):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    write_export_workspace(
        source,
        [{"output_path": "resolved/main.s", "data": "@@one\n@@two\n".encode("utf-8")}],
    )
    plan = build_qlie_corpus_plan(source, output)
    first = replace(plan.segments[0], next_segment_id=None)
    broken = replace(plan, segments=(first, *plan.segments[1:]))

    result = apply_qlie_corpus_plan(broken)

    assert result.status == "source_changed"
    assert "reciprocal" in result.reason
    assert not output.exists()


def test_plan_rejects_duplicate_segment_ids_across_manifest_items(tmp_path):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    data = b"@@same\n"
    write_export_workspace(
        source,
        [
            {
                "output_path": "resolved/one.s",
                "internal_path": "scenario\\same.s",
                "data": data,
            },
            {
                "output_path": "resolved/two.s",
                "internal_path": "scenario\\same.s",
                "data": data,
            },
        ],
    )
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"][1]["entry_index"] = manifest["items"][0]["entry_index"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = build_qlie_corpus_plan(source, output)

    assert plan.status == "blocked"
    assert "duplicate segment IDs" in plan.reason
    assert not output.exists()


def test_publish_failure_removes_staging_directory(tmp_path, monkeypatch):
    source = tmp_path / "export"
    output = tmp_path / "corpus"
    write_export_workspace(
        source,
        [{"output_path": "resolved/main.s", "data": "「fixture」\n".encode("utf-8")}],
    )
    plan = build_qlie_corpus_plan(source, output)

    def fail_write(_path, _segments):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("nagi.gameio.qlie.corpus._write_segments", fail_write)
    result = apply_qlie_corpus_plan(plan)

    assert result.status == "write_failed"
    assert not output.exists()
    assert not any(path.name.startswith(".corpus.staging-") for path in tmp_path.iterdir())


def test_existing_output_and_git_worktree_are_rejected(tmp_path):
    source = tmp_path / "export"
    existing = tmp_path / "existing"
    existing.mkdir()
    write_export_workspace(
        source,
        [{"output_path": "resolved/main.s", "data": b"@@scene\n"}],
    )
    existing_plan = build_qlie_corpus_plan(source, existing)

    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    git_plan = build_qlie_corpus_plan(source, repository / "corpus")

    assert existing_plan.status == "invalid_input"
    assert "already exists" in existing_plan.reason
    assert git_plan.status == "invalid_input"
    assert "Git worktree" in git_plan.reason
