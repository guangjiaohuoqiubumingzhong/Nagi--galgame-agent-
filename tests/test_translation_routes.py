"""Synthetic QLIE scripts exercised through the real parser and RAG pipeline."""

import hashlib
import json
import random

import pytest

from nagi.cli import run_translate_command
from nagi.gameio.qlie.flow import Instruction, Program
from nagi.gameio.qlie.script import parse_qlie_script_bytes
from nagi.gameio.segments import segments_to_jsonl
from nagi.translation.context import (
    TranslationContextBuilder,
    prepare_translation_requests,
)
from nagi.translation.planner import build_translation_batch_plan
from nagi.translation.routes import RouteAnalysis, analyze_corpus_routes


def corpus(tmp_path, scripts, *, archive_order=None):
    root = tmp_path / "corpus"
    root.mkdir()
    segments = []
    for i, (path, text) in enumerate(scripts.items()):
        spec = text if isinstance(text, dict) else {"text": text}
        result = parse_qlie_script_bytes(
            spec["text"].encode(),
            archive_name=spec.get("archive", "data.pack"),
            internal_path=spec.get("internal", path),
            output_path=path,
            entry_index=i,
        )
        assert result.status == "supported"
        segments.extend(result.segments)
    raw = segments_to_jsonl(segments).encode()
    (root / "segments.jsonl").write_bytes(raw)
    report = {
        "schema_version": 1,
        "status": "published",
        "summary": {"segment_count": len(segments), "unknown_count": 0},
        "artifacts": {
            "segments": {
                "path": "segments.jsonl",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        },
    }
    if archive_order is not None:
        manifest = json.dumps(
            {"archive_order_explicit": True, "archive_order": archive_order}
        ).encode()
        (root / "source-manifest.json").write_bytes(manifest)
        report["artifacts"]["source_manifest"] = {
            "sha256": hashlib.sha256(manifest).hexdigest()
        }
    (root / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    plan = build_translation_batch_plan(root)
    assert plan.status == "ready", plan.reason
    ids = {s.source_text: s.segment_id for s in segments}
    return root, plan, ids


def diamond():
    return {
        "scenario/root.s": (
            "@@MAIN\n「共通」\n\\if,#flag==1\n"
            '\\go,@@MAIN,"left.s"\n\\else\n'
            '\\go,@@MAIN,"right.s"\n\\end\n「死んだコード」\n'
        ),
        "scenario/left.s": '@@MAIN\n「左」\n\\go,@@MAIN,"join.s"\n',
        "scenario/right.s": '@@MAIN\n「右」\n\\go,@@MAIN,"join.s"\n',
        "scenario/join.s": "@@MAIN\n「合流」\n",
    }


def test_diamond_distinguishes_common_optional_and_unreachable_history(tmp_path):
    _, plan, ids = corpus(tmp_path, diamond())
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete, analysis.summary()
    common, left, right, join = (
        ids[t] for t in ("「共通」", "「左」", "「右」", "「合流」")
    )
    assert analysis.classify(common, join) == "must"
    assert analysis.classify(left, join) == analysis.classify(right, join) == "may"
    assert analysis.classify(left, right) == "unreachable"
    assert analysis.classify(join, left) == "unreachable"
    assert analysis.classify(ids["「死んだコード」"], join) == "unreachable"
    assert analysis.history(join)["must"] == [common]
    assert set(analysis.history(join)["may_only"]) == {left, right}


def test_nested_conditions_case_arms_and_no_default(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n「開始」\n\\case,#selection\n\\ans,1\n"
                "「一」\n\\if,#flag\n「内側」\n\\else\n「内側別」\n\\end\n"
                "\\ans,2\n「二」\n\\end\n「結末」\n"
            )
        },
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete
    end = ids["「結末」"]
    assert analysis.classify(ids["「開始」"], end) == "must"
    for text in ("「一」", "「二」", "「内側」", "「内側別」"):
        assert analysis.classify(ids[text], end) == "may"
    assert analysis.classify(ids["「一」"], ids["「内側」"]) == "must"
    assert analysis.classify(ids["「一」"], ids["「二」"]) == "unreachable"


def test_inline_commands_do_not_lose_branch_or_quoted_path(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {
            "scenario/root.s": '@@MAIN\n「開始」\n\\case,#x\n\\ans,1  \\cal,#y=1  \\go,@@MAIN,"Scenario\\a.s"\n\\else\n\\go,@@MAIN,"b.s"\n\\end\n',
            "scenario/a.s": "@@MAIN\n「甲」\n",
            "scenario/b.s": "@@MAIN\n「乙」\n",
        },
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete, analysis.summary()
    assert analysis.classify(ids["「開始」"], ids["「甲」"]) == "must"
    assert analysis.classify(ids["「甲」"], ids["「乙」"]) == "unreachable"


@pytest.mark.parametrize("command", ["sub", "jmp"])
def test_call_stack_keeps_shared_callee_returns_separate(tmp_path, command):
    _, plan, ids = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n「入口」\n\\if,#flag\n「左前」\n"
                f'\\{command},@@MAIN,"shared.s"\n「左後」\n\\else\n「右前」\n'
                f'\\{command},@@MAIN,"shared.s"\n「右後」\n\\end\n「合流」\n'
            ),
            "scenario/shared.s": "@@MAIN\n「共有」\n\\ret\n「到達不能」\n",
        },
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete
    assert analysis.classify(ids["「共有」"], ids["「合流」"]) == "must"
    assert analysis.classify(ids["「左前」"], ids["「共有」"]) == "may"
    assert analysis.classify(ids["「右前」"], ids["「共有」"]) == "may"
    assert analysis.classify(ids["「右前」"], ids["「左後」"]) == "unreachable"
    assert analysis.classify(ids["「左後」"], ids["「右後」"]) == "unreachable"
    assert analysis.history(ids["「到達不能」"])["target_status"] == "unreachable"


def test_loop_histories_converge_without_claiming_optional_iterations(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n「入口」\n@@loop\n\\if,#again\n「繰返し」\n\\go,@@loop\n\\end\n「終了」\n"
            )
        },
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete
    assert analysis.classify(ids["「入口」"], ids["「終了」"]) == "must"
    assert analysis.classify(ids["「繰返し」"], ids["「終了」"]) == "may"
    assert analysis.classify(ids["「繰返し」"], ids["「繰返し」"]) == "self"


@pytest.mark.parametrize(
    "control",
    [
        "\\go,#dynamic",
        "\\go,@@missing",
        "\\if,#x",
        "\\else",
        "\\end",
        "^unknown,1",
        "\\while,#x",
        "\\sub,@@MAIN",
    ],
)
def test_unknown_or_unbounded_control_never_produces_must_claims(tmp_path, control):
    _, plan, ids = corpus(
        tmp_path, {"scenario/root.s": "@@MAIN\n「入口」\n" + control + "\n「後」\n"}
    )
    analysis = analyze_corpus_routes(plan)
    assert not analysis.complete
    assert analysis.history(ids["「後」"])["must"] == []
    assert analysis.classify(ids["「入口」"], ids["「後」"]) == "unknown"


def test_unknown_on_one_branch_invalidates_universal_claims_on_other(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n「開始」\n\\if,#x\n\\go,#dynamic\n\\else\n「別」\n\\end\n「後」\n"
            )
        },
    )
    analysis = analyze_corpus_routes(plan)
    assert not analysis.complete
    assert analysis.classify(ids["「開始」"], ids["「後」"]) == "may"
    assert not analysis.history(ids["「後」"])["must"]


def test_unknown_in_dead_code_does_not_poison_reachable_graph(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n「開始」\n\\go,@@end\n\\mystery\n@@end\n「後」\n"
            )
        },
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete
    assert analysis.classify(ids["「開始」"], ids["「後」"]) == "must"


def test_multiple_explicit_entries_intersect_histories(tmp_path):
    _, plan, ids = corpus(tmp_path, diamond())
    analysis = analyze_corpus_routes(
        plan, entry_segment_ids=(ids["「左」"], ids["「右」"])
    )
    assert analysis.complete
    assert analysis.history(ids["「合流」"])["must"] == []
    assert analysis.classify(ids["「共通」"], ids["「合流」"]) == "unreachable"
    with pytest.raises(ValueError, match="entry"):
        analyze_corpus_routes(plan, entry_segment_ids=("missing",))


def test_missing_entry_and_resource_names_do_not_guess_a_route(tmp_path):
    _, plan, _ = corpus(
        tmp_path, {"scenario/a.s": "「甲」\n", "scenario/z.s": "「乙」\n"}
    )
    analysis = analyze_corpus_routes(plan)
    assert not analysis.complete
    assert analysis.summary()["entry_policy"] == "unknown-entry"


def test_state_limit_discards_must_results(tmp_path):
    _, plan, _ = corpus(tmp_path, diamond())
    analysis = analyze_corpus_routes(plan)
    limited = RouteAnalysis(analysis.program, analysis.ids, max_states=2)
    assert not limited.complete
    assert limited.summary()["diagnostics"][0]["reason"] == "state_limit"
    assert all(not limited.history(sid)["must"] for sid in limited.ids)


def test_rag_injects_only_must_history_across_scripts(tmp_path):
    scripts = diamond()
    scripts["scenario/root.s"] = scripts["scenario/root.s"].replace(
        "「共通」", "「silver key library gate common」"
    )
    scripts["scenario/left.s"] = scripts["scenario/left.s"].replace(
        "「左」", "「silver key library gate left」"
    )
    scripts["scenario/right.s"] = scripts["scenario/right.s"].replace(
        "「右」", "「silver key library gate right」"
    )
    scripts["scenario/join.s"] = "@@MAIN\n「silver key library gate remember」\n"
    root, _, ids = corpus(tmp_path, scripts)
    summary = {}
    _, requests, _ = prepare_translation_requests(
        root, model_id="fake", batch_size=1, summary=summary
    )
    target = next(
        u
        for r in requests
        for u in r.units
        if u.unit.segment_id == ids["「silver key library gate remember」"]
    )
    assert len(target.rag_context) == 1
    payload = json.loads(target.rag_context[0].text)
    assert payload["history"] == "all_paths"
    assert payload["passage_segment_ids"] == [ids["「silver key library gate common」"]]
    assert summary["boundary_policy"] == "must-history-only"
    assert summary["route_analysis"]["status"] == "complete"


def test_entries_change_cache_identity_and_config_roundtrips(tmp_path):
    _, plan, ids = corpus(tmp_path, diamond())
    default = TranslationContextBuilder(plan)
    selected = TranslationContextBuilder(
        plan, {"route_entry_segment_ids": [ids["「左」"]]}
    )
    assert default.index_id != selected.index_id
    assert (
        selected.config.from_dict(json.loads(json.dumps(selected.config.to_dict())))
        == selected.config
    )


def test_cli_exposes_may_must_without_source_text(tmp_path, capsys):
    root, _, ids = corpus(tmp_path, diamond())
    assert (
        run_translate_command(
            ["analyze-routes", str(root), "--segment-id", ids["「合流」"]]
        )
        == 0
    )
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["history"]["must"] == [ids["「共通」"]]
    assert report["branch_count"] == 1
    assert "共通" not in output and "合流" not in output


def test_duplicate_label_is_not_arbitrarily_resolved(tmp_path):
    _, plan, _ = corpus(
        tmp_path, {"scenario/root.s": "@@MAIN\n\\go,@@x\n@@x\n「甲」\n@@x\n「乙」\n"}
    )
    assert not analyze_corpus_routes(plan).complete


def test_inactive_archive_variant_cannot_be_an_entry(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {
            "base/root.s": {
                "text": "「旧版」\n",
                "archive": "base.pack",
                "internal": "scenario/root.s",
            },
            "patch/root.s": {
                "text": "「新版」\n",
                "archive": "patch.pack",
                "internal": "scenario/root.s",
            },
        },
        archive_order=["base.pack", "patch.pack"],
    )
    analysis = analyze_corpus_routes(plan)
    with pytest.raises(ValueError):
        analyze_corpus_routes(plan, entry_segment_ids=(ids["「旧版」"],))
    assert analysis.complete
    assert analysis.history(ids["「新版」"])["target_status"] == "reachable"
    assert analysis.history(ids["「旧版」"])["target_status"] == "unreachable"


def test_ambiguous_archive_precedence_is_rejected(tmp_path):
    _, plan, _ = corpus(
        tmp_path,
        {
            "base/root.s": {
                "text": "「旧版」\n",
                "archive": "base.pack",
                "internal": "scenario/root.s",
            },
            "patch/root.s": {
                "text": "「新版」\n",
                "archive": "patch.pack",
                "internal": "scenario/root.s",
            },
        },
    )
    with pytest.raises(ValueError, match="覆盖顺序"):
        analyze_corpus_routes(plan)


def test_inline_label_transfer_does_not_fall_into_next_line(tmp_path):
    _, plan, ids = corpus(
        tmp_path,
        {"scenario/root.s": ("@@MAIN \\go,@@last\n「不通」\n@@last\n「最後」\n")},
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.complete, analysis.summary()
    assert analysis.history(ids["「不通」"])["target_status"] == "unreachable"
    assert analysis.history(ids["「最後」"])["target_status"] == "reachable"


def test_rag_omits_all_evidence_when_flow_is_incomplete(tmp_path):
    root, _, _ = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n「silver key library gate」\n^unknown\n"
                "「待つ」\n「silver key library gate remember」\n"
            )
        },
    )
    _, requests, _ = prepare_translation_requests(root, model_id="fake", batch_size=1)
    assert all(not u.rag_context for r in requests for u in r.units)


def test_scene_label_fallthrough_is_must_not_a_new_route(tmp_path):
    _, plan, ids = corpus(
        tmp_path, {"scenario/root.s": "@@MAIN\n「教室」\n@@cafe\n「喫茶店」\n"}
    )
    analysis = analyze_corpus_routes(plan)
    assert analysis.classify(ids["「教室」"], ids["「喫茶店」"]) == "must"


def test_malformed_quote_and_duplicate_else_are_explicitly_incomplete(tmp_path):
    _, plan, _ = corpus(
        tmp_path,
        {
            "scenario/root.s": (
                "@@MAIN\n\\if,#x\n「甲」\n\\else\n「乙」\n\\else\n「丙」\n\\end\n"
            )
        },
    )
    assert not analyze_corpus_routes(plan).complete


def test_source_change_after_planning_cannot_build_routes(tmp_path):
    root, plan, _ = corpus(tmp_path, {"scenario/root.s": "「前」\n"})
    raw = (root / "segments.jsonl").read_bytes() + b"\n"
    (root / "segments.jsonl").write_bytes(raw)
    report = json.loads((root / "parse-report.json").read_text())
    report["artifacts"]["segments"]["sha256"] = hashlib.sha256(raw).hexdigest()
    (root / "parse-report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="计划不匹配"):
        analyze_corpus_routes(plan)


def test_dataflow_matches_independent_reachability_and_node_removal_oracle():
    # Independent graph definition of dominance, including cycles and nodes
    # sharing a source label (as with multiple calls to one subroutine).
    rng = random.Random(58103)
    for _ in range(35):
        labels = [f"text-{rng.randrange(7)}" for _ in range(10)]
        edges = [{j for j in range(10) if rng.random() < 0.16} for _ in range(10)]
        instructions = tuple(
            Instruction(labels[i], "branch", None, tuple(sorted(edges[i])))
            for i in range(10)
        )
        analysis = RouteAnalysis(Program(instructions, (0,), (), "test-entry"), labels)

        def reachable(starts, banned=None):
            seen, pending = set(), list(starts)
            while pending:
                node = pending.pop()
                if node in seen or labels[node] == banned:
                    continue
                seen.add(node)
                pending.extend(edges[node])
            return seen

        visited = reachable([0])
        for source in set(labels):
            source_nodes = [i for i in visited if labels[i] == source]
            later = reachable([j for i in source_nodes for j in edges[i]])
            bypass = reachable([0], banned=source)
            for target in set(labels) - {source}:
                target_nodes = {i for i in visited if labels[i] == target}
                expected = "unreachable"
                if target_nodes and not target_nodes & bypass:
                    expected = "must"
                elif target_nodes & later:
                    expected = "may"
                assert analysis.classify(source, target) == expected
