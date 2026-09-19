import json
import struct

import pytest

from nagi.gameio import yuris
from nagi.gameio.text_engines import extract_game
from nagi.translation.route_context import RouteContext, analyze_extracted_routes
from nagi.translation.routes import RouteAnalysis
from nagi.webapp import TranslationJob


def extracted(tmp_path, engine, scripts):
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.exe").write_bytes(b"MZ fixture")
    for path, text in scripts.items():
        destination = game / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(text.encode("utf-8"))
    root = tmp_path / "extract"
    root.mkdir()
    extract_game(
        game, root, TranslationJob("extract", str(game), "full", str(root)), engine
    )
    return root / "corpus"


def analyse(tmp_path, engine, scripts):
    corpus = extracted(tmp_path, engine, scripts)
    analysis, units = analyze_extracted_routes(corpus)
    return analysis, {u["text"]: u["id"] for u in units}, corpus, units


def assert_diamond(analysis, ids):
    assert analysis.complete, analysis.summary()
    assert analysis.classify(ids["共通の記憶"], ids["記憶の続き"]) == "must"
    assert analysis.classify(ids["左の記憶"], ids["記憶の続き"]) == "may"
    assert analysis.classify(ids["右の記憶"], ids["記憶の続き"]) == "may"
    assert analysis.classify(ids["左の記憶"], ids["右の記憶"]) == "unreachable"


def test_renpy_menu_diamond(tmp_path):
    analysis, ids, _, _ = analyse(
        tmp_path,
        "renpy",
        {
            "game/script.rpy": """label start:
    "共通の記憶"
    menu:
        "左":
            "左の記憶"
        "右":
            "右の記憶"
    "記憶の続き"
    return
"""
        },
    )
    assert_diamond(analysis, ids)


@pytest.mark.parametrize("engine", ["kirikiri", "tyranoscript"])
@pytest.mark.parametrize("mode", ["if", "choice"])
def test_kag_diamonds(tmp_path, engine, mode):
    fork = (
        """[if exp="f.route == 1"]
左の記憶
[else]
右の記憶
[endif]
"""
        if mode == "if"
        else """[link target=*left]左[endlink]
[link target=*right]右[endlink]
[s]
*left
左の記憶
[jump target=*join]
*right
右の記憶
*join
"""
    )
    prefix = "data/scenario/" if engine == "tyranoscript" else ""
    analysis, ids, _, _ = analyse(
        tmp_path, engine, {prefix + "first.ks": "共通の記憶\n" + fork + "記憶の続き\n"}
    )
    assert_diamond(analysis, ids)


@pytest.mark.parametrize("engine", ["renpy", "kirikiri", "tyranoscript"])
def test_cross_file_call_returns_to_caller(tmp_path, engine):
    if engine == "renpy":
        scripts = {
            "game/a.rpy": 'label start:\n    "共通の記憶"\n    call helper\n    "記憶の続き"\n    return\n',
            "game/b.rpy": 'label helper:\n    "呼び出した記憶"\n    return\n',
        }
    else:
        prefix = "data/scenario/" if engine == "tyranoscript" else ""
        scripts = {
            prefix
            + "first.ks": '共通の記憶\n[call storage="other.ks" target=*helper]\n記憶の続き\n',
            prefix + "other.ks": "*helper\n呼び出した記憶\n[return]\n",
        }
    analysis, ids, _, _ = analyse(tmp_path, engine, scripts)
    assert analysis.complete, analysis.summary()
    assert analysis.classify(ids["呼び出した記憶"], ids["記憶の続き"]) == "must"
    assert analysis.classify(ids["記憶の続き"], ids["呼び出した記憶"]) == "unreachable"


@pytest.mark.parametrize("engine", ["renpy", "kirikiri", "tyranoscript"])
def test_unknown_dynamic_jump_cannot_claim_must(tmp_path, engine):
    scripts = (
        {
            "game/script.rpy": 'label start:\n    "共通の記憶"\n    jump expression route\n    "記憶の続き"\n'
        }
        if engine == "renpy"
        else {
            (
                "data/scenario/first.ks" if engine == "tyranoscript" else "first.ks"
            ): "共通の記憶\n[jump target=&f.route]\n記憶の続き\n"
        }
    )
    analysis, ids, _, _ = analyse(tmp_path, engine, scripts)
    assert not analysis.complete
    assert analysis.history(ids["記憶の続き"])["must"] == []


def test_renpy_nested_if_loop_and_local_label(tmp_path):
    analysis, ids, _, _ = analyse(
        tmp_path,
        "renpy",
        {
            "game/script.rpy": """label start:
    "共通の記憶"
    while repeat:
        if first:
            "左の記憶"
            continue
        elif second:
            "右の記憶"
            break
        else:
            pass
    jump .done
label .done:
    "記憶の続き"
    return
"""
        },
    )
    assert analysis.complete, analysis.summary()
    assert analysis.classify(ids["共通の記憶"], ids["記憶の続き"]) == "must"
    assert analysis.classify(ids["左の記憶"], ids["記憶の続き"]) == "may"


def test_script_only_mutation_invalidates_routes(tmp_path):
    _, _, corpus, _ = analyse(
        tmp_path, "renpy", {"game/script.rpy": 'label start:\n    "本文"\n    return\n'}
    )
    snapshot = next((corpus.parent / "script-export/documents").glob("*.rpy"))
    snapshot.write_bytes(snapshot.read_bytes().replace(b"return", b"jump absent"))
    with pytest.raises(ValueError, match="snapshot changed"):
        analyze_extracted_routes(corpus)


@pytest.mark.parametrize("hybrid", [False, True])
def test_actual_translation_messages_use_only_must_and_resume(tmp_path, monkeypatch, hybrid):
    from nagi.translation import text_engines
    from nagi.translation.yuris import batches

    analysis, ids, corpus, units = analyse(
        tmp_path,
        "renpy",
        {
            "game/script.rpy": """label start:
    "共通の記憶"
    if route:
        "左の記憶"
    else:
        "右の記憶"
    "共通の記憶の続き"
    return
"""
        },
    )
    monkeypatch.setattr(text_engines, "batches", lambda rows: batches(rows, size=1))
    captured = []

    class Client:
        def complete(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            captured.append(payload)
            return json.dumps(
                {
                    "translations": [
                        {"id": u["id"], "text": "中"} for u in payload["texts"]
                    ]
                }
            )

    job = TranslationJob(
        "translate",
        game_dir=str(tmp_path / "game"),
        mode="full",
        output_root=str(tmp_path / "result"),
    )
    if hybrid:
        from test_translation_context import FakeEmbedder

        job.retrieval_models = (FakeEmbedder(), None, ())
    text_engines.translate(job, corpus, Client, "renpy", workers=1)
    assert job.context_summary["retrieval_mode"] == ("hybrid" if hybrid else "bm25")
    target = next(p for p in captured if p["texts"][0]["id"] == ids["共通の記憶の続き"])
    assert target["route_context"]["references"][ids["共通の記憶の続き"]] == [
        ids["共通の記憶"]
    ]
    assert all(
        row["history"] == "all_paths"
        for row in target["route_context"]["evidence_pool"]
    )
    count = len(captured)
    text_engines.translate(job, corpus, Client, "renpy", workers=1)
    assert len(captured) == count
    assert job.context_summary["route_analysis"]["status"] == "complete"
    context = RouteContext(analysis, units)
    assert (
        context.payload([{"id": ids["共通の記憶"], "text": "記憶"}])["evidence_pool"]
        == []
    )


def yuris_program(rows):
    """Real encrypted YSTB, command table and decoded text fields."""
    commands = list(dict.fromkeys(command for command, _ in rows))
    table = [
        (command, ["#" if command in {"GO", "GOSUB"} else "STR"])
        for command in commands
    ]
    code, args, resources = bytearray(), bytearray(), bytearray()
    for command, text in rows:
        code.extend(
            struct.pack("<BBH", commands.index(command), int(text is not None), 0)
        )
        if text is not None:
            raw = text.encode("cp932")
            typ = 0 if command == "WORD" else 3
            if typ:
                raw = b'"' + raw + b'"'
                raw = struct.pack("<BH", 0x4D, len(raw)) + raw
            args.extend(struct.pack("<HBBII", 0, typ, 0, len(raw), len(resources)))
            resources.extend(raw)
    key = bytes.fromhex("d36fac96")
    data = struct.pack(
        "<4s7I",
        b"YSTB",
        479,
        len(rows),
        len(code),
        len(args),
        len(resources),
        len(code),
        0,
    )
    data += b"".join(
        yuris.crypt(part, key) for part in (code, args, resources, bytes(len(code)))
    )
    path = "ysbin/yst00001.ybn"
    units = [
        {
            "id": str(i),
            "script": path,
            "instruction": i,
            "command": command,
            "text": text,
        }
        for i, (command, text) in enumerate(rows)
        if command == "WORD"
    ]
    from nagi.gameio.yuris_flow import build_program

    program = build_program([{"name": path, "content": data}], table, key, units)
    return RouteAnalysis(program, [u["id"] for u in units]), {
        u["text"]: u["id"] for u in units
    }


def test_yuris_bytecode_diamond_and_loop():
    analysis, ids = yuris_program(
        [
            ("WORD", "共通の記憶"),
            ("IF", None),
            ("WORD", "左の記憶"),
            ("ELSE", None),
            ("WORD", "右の記憶"),
            ("IFEND", None),
            ("WORD", "記憶の続き"),
            ("LOOP", None),
            ("WORD", "繰り返す記憶"),
            ("LOOPEND", None),
            ("WORD", "終わり"),
            ("END", None),
        ]
    )
    assert_diamond(analysis, ids)
    assert analysis.classify(ids["繰り返す記憶"], ids["終わり"]) == "may"
    assert analysis.classify(ids["記憶の続き"], ids["終わり"]) == "must"


def test_yuris_real_archive_cross_script_labels():
    from test_opening_selection import story_archive

    from nagi.gameio.yuris_flow import build_program

    entries = yuris.archive_entries(story_archive(count=9, opening_command="GOSUB"))
    table, key, units = yuris.catalog(entries)
    analysis = RouteAnalysis(
        build_program(entries, table, key, units), [u["id"] for u in units]
    )
    ids = {u["text"]: u["id"] for u in units}
    assert analysis.complete, analysis.summary()
    assert analysis.classify(ids["開場本文0"], ids["開場本文8"]) == "must"
    assert analysis.classify(ids["無関係な後半"], ids["開場本文8"]) == "unreachable"


@pytest.mark.parametrize(
    "control",
    [
        "[jump target=*absent]",
        "[eval exp='f.x=1']",
        "[if exp='f.x']",
        "[return cond='f.x']",
    ],
)
def test_kag_unknown_or_malformed_control_fails_closed(tmp_path, control):
    analysis, ids, _, _ = analyse(
        tmp_path, "kirikiri", {"first.ks": "共通の記憶\n" + control + "\n記憶の続き\n"}
    )
    assert not analysis.complete
    assert analysis.history(ids["記憶の続き"])["must"] == []


def test_kag_nested_elsif_and_conditional_call(tmp_path):
    analysis, ids, _, _ = analyse(
        tmp_path,
        "kirikiri",
        {
            "first.ks": """共通の記憶
@if exp="f.x"
@if exp="f.y"
左の記憶
@endif
@elsif exp="f.z"
右の記憶
@else
別の記憶
@endif
[call storage=other.ks cond="f.x"]
記憶の続き
""",
            "other.ks": "呼び出した記憶\n[return]\n",
        },
    )
    assert_diamond(analysis, ids)
    assert analysis.classify(ids["呼び出した記憶"], ids["記憶の続き"]) == "may"


@pytest.mark.parametrize("engine", ["kirikiri", "tyranoscript"])
def test_choice_can_be_clicked_before_wait(tmp_path, engine):
    prefix = "data/scenario/" if engine == "tyranoscript" else ""
    analysis, ids, _, _ = analyse(
        tmp_path,
        engine,
        {
            prefix + "first.ks": """共通の記憶
[link target=*end]選択[endlink]
まだ表示されていない記憶
[s]
*end
記憶の続き
"""
        },
    )
    assert analysis.complete, analysis.summary()
    assert (
        analysis.classify(ids["まだ表示されていない記憶"], ids["記憶の続き"]) == "may"
    )


def test_inline_transfer_does_not_make_whole_record_history(tmp_path):
    analysis, _, _, _ = analyse(
        tmp_path,
        "kirikiri",
        {"first.ks": "前文[jump target=*end]未表示の文章\n*end\n本文\n"},
    )
    assert not analysis.complete


def test_live_menu_state_across_call_is_not_guessed(tmp_path):
    analysis, _, _, _ = analyse(tmp_path, "kirikiri", {
        "first.ks": '[link target=*end]選択[endlink]\n[call storage=other.ks]\n[s]\n*end\n本文\n',
        "other.ks": '前文\n[return]\n'})
    assert not analysis.complete


@pytest.mark.parametrize("command", ["GOSUB", "GO", "IF", "SWITCH"])
def test_yuris_unknown_control_clears_must(command):
    analysis, ids = yuris_program(
        [
            ("WORD", "前文"),
            (command, "MISSING" if command in {"GO", "GOSUB"} else None),
            ("WORD", "本文"),
            ("END", None),
        ]
    )
    assert not analysis.complete
    assert analysis.history(ids["本文"])["must"] == []


def test_yuris_else_expression_has_false_edge():
    analysis, ids = yuris_program(
        [
            ("WORD", "前文"),
            ("IF", None),
            ("WORD", "分岐一"),
            ("ELSE", "opaque_condition"),
            ("WORD", "分岐二"),
            ("ELSE", None),
            ("WORD", "分岐三"),
            ("IFEND", None),
            ("WORD", "本文"),
            ("END", None),
        ]
    )
    assert analysis.complete, analysis.summary()
    assert analysis.classify(ids["前文"], ids["本文"]) == "must"
    for name in ("分岐一", "分岐二", "分岐三"):
        assert analysis.classify(ids[name], ids["本文"]) == "may"


def test_yuris_break_level_is_not_silently_ignored():
    analysis, ids = yuris_program(
        [
            ("WORD", "前文"),
            ("LOOP", None),
            ("LOOP", None),
            ("LOOPBREAK", "2"),
            ("LOOPEND", None),
            ("LOOPEND", None),
            ("WORD", "本文"),
            ("END", None),
        ]
    )
    assert not analysis.complete
    assert analysis.history(ids["本文"])["must"] == []


def test_explicit_entry_in_unknown_renpy_block_stays_unknown(tmp_path):
    _, ids, corpus, _ = analyse(
        tmp_path,
        "renpy",
        {
            "game/script.rpy": 'label start:\n    return\nlabel helper:\n    custom:\n        "本文"\n        jump absent\n'
        },
    )
    analysis, _ = analyze_extracted_routes(corpus, entry_ids=[ids["本文"]])
    assert not analysis.complete


def test_text_engine_cli_report_has_history_without_source_text(tmp_path, capsys):
    from nagi.cli import run_translate_command

    _, ids, corpus, _ = analyse(
        tmp_path,
        "renpy",
        {"game/script.rpy": 'label start:\n    "前文"\n    "本文"\n    return\n'},
    )
    assert (
        run_translate_command(
            ["analyze-routes", str(corpus), "--segment-id", ids["本文"]]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "前文" not in output and "本文" not in output
    assert json.loads(output)["history"]["must"] == [ids["前文"]]


def test_legacy_text_translation_preserves_paid_request(tmp_path):
    from nagi.translation.route_context import digest
    from nagi.translation.text_engines import translate
    from nagi.translation.yuris import batch_messages, batches

    _, _, corpus, units = analyse(
        tmp_path, "renpy", {"game/script.rpy": 'label start:\n    "本文"\n    return\n'}
    )
    root = tmp_path / "result"
    job = TranslationJob("legacy", str(tmp_path / "game"), "full", str(root))
    calls = []

    class Client:
        def complete(self, messages, **kwargs):
            calls.append(messages)
            return json.dumps(
                {"translations": [{"id": units[0]["id"], "text": "译文"}]}
            )

    translate(job, corpus, Client, "renpy", workers=1)
    plan = json.loads((root / "translation-plan.json").read_text(encoding="utf-8"))
    del plan["route_context_id"]
    (root / "translation-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    receipt_path = root / "api-batches/00000.response.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    import hashlib

    receipt["request_sha256"] = hashlib.sha256(
        json.dumps(batch_messages(next(batches(units))), ensure_ascii=False).encode()
    ).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = digest(plan)
    translate(job, corpus, Client, "renpy", workers=1)
    assert len(calls) == 1
    assert (
        digest(json.loads((root / "translation-plan.json").read_text(encoding="utf-8")))
        == before
    )


@pytest.mark.parametrize("engine", ["renpy", "kirikiri", "tyranoscript", "yuris-479"])
@pytest.mark.parametrize("rerank", [False, True])
def test_all_engines_hybrid_evidence_respects_routes_and_cache(tmp_path, engine, rerank):
    from test_translation_context import FakeEmbedder, FakeReranker

    from nagi.rag.semantic import RetrievalCache
    from nagi.translation.yuris import batch_messages

    common = "silver key library gate"
    left, right = "silver key left", "silver key right"
    target = "promise remembered" if rerank else "silver key remembered"
    if engine == "yuris-479":
        analysis, ids = yuris_program([
            ("WORD", common), ("IF", None), ("WORD", left),
            ("ELSE", None), ("WORD", right), ("IFEND", None),
            ("WORD", target), ("END", None),
        ])
        units = [{"id": sid, "text": text, "command": "WORD"}
                 for text, sid in ids.items()]
    else:
        scripts = {"game/script.rpy": (
            f'label start:\n    "{common}"\n    if route:\n        "{left}"\n'
            f'    else:\n        "{right}"\n    "{target}"\n    return\n'
        )} if engine == "renpy" else {
            ("data/scenario/first.ks" if engine == "tyranoscript" else "first.ks"):
            f'{common}\n[if exp="f.route"]\n{left}\n[else]\n{right}\n[endif]\n{target}\n'
        }
        analysis, ids, _, units = analyse(tmp_path, engine, scripts)
    embedder, reranker = FakeEmbedder(), FakeReranker()
    context = RouteContext(analysis, units, embedder=embedder, reranker=reranker if rerank else None,
                           cache=RetrievalCache(tmp_path / "vectors.sqlite3"))
    batch = [{"id": ids[target], "text": target}]
    messages = batch_messages(batch, route_context=context)
    payload = json.loads(messages[-1]["content"])["route_context"]
    assert payload["references"] == {ids[target]: [ids[common]]}
    assert {e["id"] for e in payload["evidence_pool"]} == {ids[common]}
    assert reranker.calls == ([(target, [common])] if rerank else [])
    assert any(query for query, _ in embedder.calls)
    calls = len(embedder.calls), len(reranker.calls)
    assert batch_messages(batch, route_context=context) == messages
    assert (len(embedder.calls), len(reranker.calls)) == calls
    assert context.summary()["retrieval_mode"] == "hybrid"
    assert len(json.dumps(payload, ensure_ascii=False)) <= 3000
    # Even a semantic match cannot retrieve another target from the same batch.
    assert not context.payload([batch[0], {"id": ids[common], "text": common}])["evidence_pool"]


def test_hybrid_resume_identity_and_legacy_bm25(tmp_path, monkeypatch):
    from test_translation_context import FakeEmbedder

    from nagi.rag import semantic
    from nagi.translation.context import TranslationContextConfig
    from nagi.translation.route_context import prepare_route_context

    analysis, _, corpus, units = analyse(tmp_path, "renpy", {
        "game/script.rpy": 'label start:\n    "silver key"\n    "silver key remembered"\n    return\n'
    })
    monkeypatch.setenv("NAGI_DATA_DIR", str(tmp_path / "state"))
    job = TranslationJob("rag", str(tmp_path / "game"), "full", str(tmp_path / "result"))
    job.retrieval_models = (FakeEmbedder(), None, ())
    context = prepare_route_context(corpus, None, job=job)
    previous = {"route_context_id": context.identity}
    assert prepare_route_context(corpus, previous, job=job).identity == context.identity
    job.context_config = TranslationContextConfig(semantic_min_score=0.9)
    with pytest.raises(ValueError, match="Saved route context differs"):
        prepare_route_context(corpus, previous, job=job)
    job.context_config = TranslationContextConfig()
    job.retrieval_models = (None, None, ())
    with pytest.raises(ValueError, match="Saved route context differs"):
        prepare_route_context(corpus, previous, job=job)

    legacy = RouteContext(analysis, units, legacy=True)
    def no_models():
        raise AssertionError("legacy runs must never load new models")
    monkeypatch.setattr(semantic, "local_models", no_models)
    job.retrieval_models = None
    resumed = prepare_route_context(corpus, {"route_context_id": legacy.identity}, job=job)
    assert resumed.identity == legacy.identity
    assert resumed.payload([units[-1]]) == legacy.payload([units[-1]])
    assert resumed.summary()["retrieval_mode"] == "bm25"
    assert prepare_route_context(corpus, {}, job=job) is None


def test_route_hybrid_bounds_and_disabled_budget(tmp_path):
    from test_translation_context import FakeEmbedder

    from nagi.translation.context import TranslationContextConfig

    long_text = "silver key " * 200
    analysis, _, _, units = analyse(tmp_path, "renpy", {
        "game/script.rpy": f'label start:\n    "{long_text}"\n    "silver key remembered"\n    return\n'
    })
    embedder = FakeEmbedder()
    context = RouteContext(analysis, units, embedder=embedder)
    assert all(long_text not in texts for _, texts in embedder.calls)
    assert context.payload([units[-1]])["evidence_pool"][0]["text"] == long_text
    disabled = RouteContext(analysis, units, embedder=embedder,
                            config=TranslationContextConfig(rag_budget_chars=0))
    assert disabled.vectors == {}
    assert disabled.payload([units[-1]]) == {"evidence_pool": [], "references": {}}
