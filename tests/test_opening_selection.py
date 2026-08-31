import struct

import pytest
from test_qlie_corpus import write_export_workspace

from nagi.gameio import yuris
from nagi.gameio.qlie.corpus import apply_qlie_corpus_plan, build_qlie_corpus_plan
from nagi.gameio.qlie.opening import select_opening as qlie_opening
from nagi.gameio.yuris_opening import select_opening, selected_for_result
from nagi.translation.planner import build_translation_batch_plan


def story_archive(count=75, *, next_target="CONTINUE", opening_command="GO"):
    """Archive order is deliberately unrelated to PC/label execution order."""
    commands = [("WORD", ["STR"]), ("GO", ["#"]), ("END", []),
                ("GOSUB", ["#"]), ("RETURN", []), ("IF", [])]
    table = bytearray(b"YSCM" + struct.pack("<III", 479, len(commands), 0))
    for name, params in commands:
        table.extend(name.encode() + b"\0" + bytes([len(params)]))
        for param in params:
            table.extend(param.encode() + b"\0\0\0")
    key = bytes.fromhex("d36fac96")

    def script(rows):
        code, args, resources = bytearray(), bytearray(), bytearray()
        for command, text in rows:
            opcode = next(i for i, row in enumerate(commands) if row[0] == command)
            code.extend(struct.pack("<BBH", opcode, int(text is not None), 0))
            if text is None:
                continue
            raw = text.encode("cp932")
            typ = 0 if command == "WORD" else 3
            if typ == 3:
                raw = b'"' + raw + b'"'
                raw = struct.pack("<BH", 0x4D, len(raw)) + raw
            args.extend(struct.pack("<HBBII", 0, typ, 0, len(raw), len(resources)))
            resources.extend(raw)
        return struct.pack("<4s7I", b"YSTB", 479, len(rows), len(code), len(args), len(resources), len(code), 0) + b"".join(
            yuris.crypt(part, key) for part in (code, args, resources, bytes(len(code))))

    first = [("WORD", f"開場本文{i}") for i in range(min(3, count))]
    rest = [("WORD", f"開場本文{i}") for i in range(3, count)]
    labels = {"SCENARIO_MAIN": (90, 0), "OPENING": (30, 0), "CONTINUE": (70, 0)}
    label_data = bytearray(b"YSLB" + struct.pack("<II", 479, len(labels),) + bytes(256 * 4))
    for name, (number, pc) in labels.items():
        raw = name.encode()
        label_data.extend(bytes([len(raw)]) + raw + struct.pack("<III", 0, pc, number))
    files = [("ysbin/ysc.ybn", bytes(table)), ("ysbin/ysl.ybn", bytes(label_data)),
             ("ysbin/yst00001.ybn", script([("WORD", "無関係な後半"), ("END", None)])),
             ("ysbin/yst00070.ybn", script(rest + [("END", None)])),
             ("ysbin/yst00030.ybn", script(first + [("GO", next_target), ("WORD", "到達しない文章"), ("END", None)])),
             ("ysbin/yst00090.ybn", script([(opening_command, "OPENING" if opening_command != "IF" else None), ("END", None)]))]
    return bytes(yuris.pack_archive([{"name": name, "raw_name": name.encode(), "kind": 0,
                                     "compressed": 1, "content": data} for name, data in files]))


def selection(source):
    entries = yuris.archive_entries(source)
    table, key, units = yuris.catalog(entries)
    return select_opening(entries, table, key, units)


@pytest.mark.parametrize("count", [3, 9, 49, 50, 75])
def test_yuris_follows_labels_across_scripts_and_counts_only_story(count):
    selected, receipt = selection(story_archive(count))
    assert [u["text"] for u in selected] == [f"開場本文{i}" for i in range(min(50, count))]
    assert receipt["selected_ids"] == [u["id"] for u in selected]
    assert receipt["policy"] == "opening-story-v1"


@pytest.mark.parametrize("kwargs", [{"next_target": "MISSING"}, {"next_target": "OPENING"}, {"opening_command": "IF"}])
def test_yuris_rejects_ambiguous_flow_instead_of_archive_fallback(kwargs):
    with pytest.raises(ValueError, match="开场"):
        selection(story_archive(**kwargs))


def test_yuris_receipt_is_bound_and_legacy_result_keeps_original_selection():
    source = story_archive()
    entries = yuris.archive_entries(source)
    table, key, units = yuris.catalog(entries)
    selected, receipt = select_opening(entries, table, key, units)
    assert selected_for_result(entries, table, key, units, {}) == units[:50]
    assert selected_for_result(entries, table, key, units, {"opening_selection": receipt}) == selected
    receipt["selected_ids"].reverse()
    with pytest.raises(ValueError, match="不匹配"):
        selected_for_result(entries, table, key, units, {"opening_selection": receipt})


def qlie_corpus(tmp_path, *, opening_count=60, destination="z-opening.s", control=""):
    export, corpus = tmp_path / "export", tmp_path / "corpus"
    rows = [
        ("scenario/a-later.s", "@@MAIN\n「後半の文章」\n"),
        ("scenario/root.s", f'@@MAIN\n\\go,@@Top,"Scenario\\{destination}"\n'),
        ("scenario/z-opening.s", "@@MAIN\n" + control + "".join(f"〖花梨〗\n「開場本文{i}」\n" for i in range(opening_count))),
    ]
    write_export_workspace(export, [{"output_path": path, "internal_path": path, "data": text.encode("utf-8")}
                                   for path, text in rows])
    plan = build_qlie_corpus_plan(export, corpus)
    assert plan.status == "ready", plan.reason
    assert apply_qlie_corpus_plan(plan).status == "published"
    return corpus


@pytest.mark.parametrize("count", [4, 49, 70])
def test_qlie_root_jump_skips_system_records_and_separate_name_fields(tmp_path, count):
    corpus = qlie_corpus(tmp_path, opening_count=count)
    receipt = qlie_opening(corpus)
    assert len(receipt["selected_ids"]) == len(receipt["display_ids"]) == min(count, 50)
    plan = build_translation_batch_plan(corpus, selected_segment_ids=receipt["selected_ids"])
    assert plan.status == "ready", plan.reason
    assert [u.source_text for b in plan.batches for u in b.units] == [f"「開場本文{i}」" for i in range(min(50, count))]
    assert "selected_segment_ids" not in build_translation_batch_plan(corpus).config.to_dict()
    bad = build_translation_batch_plan(corpus, selected_segment_ids=[*receipt["selected_ids"], "absent"])
    assert bad.status == "invalid_input"


def test_qlie_missing_jump_target_is_not_silently_replaced_with_first_file(tmp_path):
    corpus = qlie_corpus(tmp_path, destination="missing.s")
    with pytest.raises(ValueError, match="目标脚本缺失"):
        qlie_opening(corpus)


def test_qlie_source_mutation_is_rejected(tmp_path):
    corpus = qlie_corpus(tmp_path)
    with (corpus / "segments.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="语料已改变"):
        qlie_opening(corpus)
