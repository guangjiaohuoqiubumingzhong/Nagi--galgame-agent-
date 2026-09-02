import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_qlie_corpus import write_export_workspace
from test_yuris_speakers import speaker_archive

from nagi import webapp
from nagi.gameio import yuris
from nagi.gameio.characters import catalogue, character, load_qlie_characters
from nagi.gameio.deployment import yuris_assets
from nagi.gameio.qlie.corpus import apply_qlie_corpus_plan, build_qlie_corpus_plan
from nagi.translation.characters import GLOSSARY_FILE, load_glossary, prepare_glossary
from nagi.translation.context import (
    TranslationContextConfig,
    prepare_translation_requests,
)
from nagi.translation.yuris import batch_messages, batches, translate
from nagi.webapp import TranslationJob


def job_at(tmp_path, *, output='full-a', game='game', mode='full', **config):
    return TranslationJob('names', str(tmp_path / game), mode, str(tmp_path / output),
                          model_config={'provider': 'fake', 'model': 'fake', 'base_url': 'https://one.invalid',
                                        'api_key': 'do-not-persist-secret', **config})


def name_reply(messages):
    rows = json.loads(messages[1]['content'])['characters']
    return json.dumps({'translations': [{'id': r['id'], 'text': '译名' + str(i)} for i, r in enumerate(rows)]})


def fixture_catalog():
    rows = [character('QLIE', n, n) for n in ('Alice', '六花', '将')]
    return catalogue('QLIE', 'a' * 64, rows, {'line-a': {'character_id': rows[0]['id'], 'role': 'dialogue'},
                                           'line-b': {'character_id': rows[2]['id'], 'role': 'dialogue'}})


def test_name_cache_is_per_game_model_and_snapshot_is_immutable(tmp_path):
    calls = []
    client = SimpleNamespace(complete=lambda messages, **kw: (calls.append(messages) or name_reply(messages)))
    catalog = fixture_catalog()
    corpus = tmp_path / 'translation-source/corpus'
    first = job_at(tmp_path)
    glossary = prepare_glossary(first, corpus, catalog, lambda: client)
    assert len(calls) == 1 and first.character_count == 3
    assert 'do-not-persist-secret' not in (Path(first.output_root) / GLOSSARY_FILE).read_text(encoding='utf-8')
    def no_api():
        pytest.fail('Stored names must not be paid for again')
    again = prepare_glossary(first, corpus, catalog, no_api)
    assert again.version == glossary.version
    second = job_at(tmp_path, output='partial-b', mode='partial')
    assert prepare_glossary(second, corpus, catalog, no_api).version == glossary.version
    changed_model = job_at(tmp_path, output='full-c', base_url='https://two.invalid')
    assert prepare_glossary(changed_model, corpus, catalog, lambda: client).version != glossary.version
    changed_game = job_at(tmp_path, output='full-d', game='another-game')
    assert prepare_glossary(changed_game, corpus, catalog, lambda: client).version != glossary.version
    assert len(calls) == 3
    first.model_config['model'] = 'other'
    with pytest.raises(ValueError, match='配置已改变'):
        prepare_glossary(first, corpus, catalog, no_api)
    path = Path(second.output_root) / GLOSSARY_FILE
    modified = json.loads(path.read_text(encoding='utf-8'))
    modified['entries'][0]['target_name'] = '被修改'
    yuris.save_json(path, modified)
    with pytest.raises(ValueError, match='校验失败'):
        prepare_glossary(second, corpus, catalog, no_api)


def test_exact_name_retrieval_does_not_guess_aliases_or_single_character_words(tmp_path):
    catalog = fixture_catalog()
    glossary = prepare_glossary(job_at(tmp_path), tmp_path / 'corpus', catalog,
                                lambda: SimpleNamespace(complete=lambda m, **kw: name_reply(m)))
    assert glossary.relevant([{'id': 'unbound', 'text': '将来去学校。Aliceland is elsewhere.'}]) == []
    found = glossary.relevant([{'id': 'unbound', 'text': 'Alice 和六花在哪里？'}])
    assert {c['lookup_name'] for c in found} == {'Alice', '六花'}
    assert {c['lookup_name'] for c in glossary.relevant([{'id': 'line-b', 'text': '早上好'}])} == {'将'}
    assert glossary.relevant([{'id': 'unbound', 'text': '小六在哪里？'}]) == []


@pytest.mark.parametrize('bad', ['{"translations":[]}', '{invalid json}', None, 'empty', 'duplicate'])
def test_invalid_names_stop_before_body_and_can_resume(tmp_path, bad):
    catalog, calls = fixture_catalog(), []
    job = job_at(tmp_path)
    def malformed(messages, **kw):
        calls.append('names')
        if bad == 'empty':
            return json.dumps({'translations': [{'id': r['id'], 'text': ''} for r in catalog['characters']]})
        if bad == 'duplicate':
            row = catalog['characters'][0]
            return json.dumps({'translations': [{'id': row['id'], 'text': '中文'}] * 3})
        return bad
    with pytest.raises((ValueError, TypeError)):
        prepare_glossary(job, tmp_path / 'corpus', catalog, lambda: SimpleNamespace(complete=malformed))
    assert not (Path(job.output_root) / GLOSSARY_FILE).exists()
    assert len(calls) <= 2
    good = prepare_glossary(job, tmp_path / 'corpus', catalog,
                            lambda: SimpleNamespace(complete=lambda m, **kw: name_reply(m)))
    assert len(good.entries) == 3


def test_cancel_after_name_response_resumes_from_durable_receipt(tmp_path):
    job = job_at(tmp_path)
    catalog = fixture_catalog()
    def reply_then_cancel(messages, **kwargs):
        job.cancel_event.set()
        return name_reply(messages)
    with pytest.raises(InterruptedError):
        prepare_glossary(job, tmp_path / 'corpus', catalog, lambda: SimpleNamespace(complete=reply_then_cancel))
    assert not (Path(job.output_root) / GLOSSARY_FILE).exists()
    job.cancel_event.clear()
    glossary = prepare_glossary(job, tmp_path / 'corpus', catalog,
                                lambda: pytest.fail('Already received names must be reused'))
    assert len(glossary.entries) == 3


def test_missing_snapshot_and_source_changes_do_not_retranslate_names(tmp_path):
    job, catalog = job_at(tmp_path), fixture_catalog()
    corpus = tmp_path / 'corpus'
    prepare_glossary(job, corpus, catalog, lambda: SimpleNamespace(complete=lambda m, **kw: name_reply(m)))
    root = Path(job.output_root)
    yuris.save_json(root / 'translation-plan.json', {'body_already_started': True})
    (root / GLOSSARY_FILE).unlink()
    with pytest.raises(ValueError, match='快照缺失'):
        prepare_glossary(job, corpus, catalog, lambda: pytest.fail('Missing snapshot must not cause paid calls'))
    changed = catalogue('QLIE', 'b' * 64, catalog['characters'], catalog['bindings'])
    with pytest.raises(ValueError, match='配置已改变'):
        prepare_glossary(job, corpus, changed, lambda: pytest.fail('Changed source must not cause paid calls'))


def test_yuris_failed_name_phase_never_submits_body_and_body_retry_reuses_names(tmp_path):
    job = job_at(tmp_path)
    game = Path(job.game_dir)
    (game / 'pac').mkdir(parents=True)
    (game / 'game.exe').write_bytes(b'MZ synthetic YU-RIS executable')
    (game / 'pac/ysbin.ypf').write_bytes(speaker_archive())
    _, corpus = yuris.extract_game(game, tmp_path / 'translation-source', job)
    calls = []
    def bad_names(messages, **kw):
        assert 'characters' in json.loads(messages[1]['content'])
        calls.append('bad-names')
        return '{"translations":[]}'
    with pytest.raises(ValueError, match='正文未提交'):
        translate(job, corpus, lambda: SimpleNamespace(complete=bad_names), workers=1)
    assert calls == ['bad-names', 'bad-names']
    def then_fail_body(messages, **kw):
        payload = json.loads(messages[1]['content'])
        if 'characters' in payload:
            calls.append('good-names')
            return name_reply(messages)
        calls.append('failed-body')
        raise RuntimeError('fixture offline')
    with pytest.raises(RuntimeError, match='offline'):
        translate(job, corpus, lambda: SimpleNamespace(complete=then_fail_body), workers=1)
    def resume_body(messages, **kw):
        payload = json.loads(messages[1]['content'])
        assert 'characters' not in payload
        assert payload['character_glossary']['entries']
        calls.append('good-body')
        return json.dumps({'translations': [{'id': u['id'], 'text': u['text']} for u in payload['texts']]})
    translate(job, corpus, lambda: SimpleNamespace(complete=resume_body), workers=1)
    assert job.status == 'completed' and calls.count('good-names') == 1


@pytest.mark.parametrize('mode', ['full', 'partial'])
def test_yuris_names_precede_body_and_drive_display_fields_outside_partial_scope(tmp_path, mode):
    source = speaker_archive(extra_dialogue=65, registry_last=True)
    job = job_at(tmp_path, mode=mode)
    game = Path(job.game_dir)
    (game / 'pac').mkdir(parents=True)
    (game / 'game.exe').write_bytes(b'MZ synthetic YU-RIS executable')
    (game / 'pac/ysbin.ypf').write_bytes(source)
    _, corpus = yuris.extract_game(game, tmp_path / 'translation-source', job)
    catalog = json.loads((corpus / 'characters.json').read_text(encoding='utf-8'))
    assert {c['lookup_name'] for c in catalog['characters']} == {'一房', '女子１'}
    assert job.character_count == 2
    assert not list(corpus.rglob('*.response.json'))  # Extraction has no paid phase.
    units = json.loads((corpus / 'texts.json').read_text(encoding='utf-8'))
    selected = units if mode == 'full' else units[:50]
    calls, body_ids = [], []
    def respond(messages, **kwargs):
        payload = json.loads(messages[1]['content'])
        if 'characters' in payload:
            calls.append('names')
            return name_reply(messages)
        assert calls and calls[0] == 'names'
        calls.append('body')
        assert payload['character_glossary']['version'].startswith('names_v1_')
        body_ids.extend(r['id'] for r in payload['texts'])
        rows = []
        for row in payload['texts']:
            binding = catalog['bindings'].get(row['id'], {})
            text = '错误姓名“你好。”' if binding.get('role') == 'dialogue' else '正文'
            rows.append({'id': row['id'], 'text': text})
        return json.dumps({'translations': rows})
    translate(job, corpus, lambda: SimpleNamespace(complete=respond), workers=1)
    assert body_ids == [u['id'] for u in selected]
    assert calls.count('names') == 1 and job.total_units == len(selected)
    glossary = load_glossary(Path(job.output_root) / GLOSSARY_FILE)
    result, packed, mapping = yuris_assets(job.output_root, game)
    assert result['character_glossary_version'] == glossary.version
    glyphs = json.loads(mapping)['glyphs']
    table, key, _ = yuris.catalog(yuris.archive_entries(source))
    output_fields = {}
    for entry in yuris.archive_entries(packed):
        if entry['content'][:4] != b'YSTB':
            continue
        for ins in yuris.parse_script(entry['content'], key, table)[0]:
            for f in ins['fields']:
                text = yuris.literal(f['blob']) if f['type'] == 3 else f['blob'].decode('cp932')
                if text is not None:
                    output_fields[f"{entry['name']}:{f['argument']}"] = ''.join(glyphs.get(c, c) for c in text)
    for identifier, binding in catalog['bindings'].items():
        entry = glossary.entries[binding['character_id']]
        if binding['role'] == 'lookup':
            assert output_fields[identifier] == entry['lookup_name']
        if binding['role'] == 'display':
            assert output_fields[identifier] == entry['target_name']
    for unit in units:
        if unit['command'] == 'WORD' and unit not in selected:
            assert output_fields[unit['id']] == unit['text']
    assert output_fields[units[0]['id']].startswith('一房「')
    saved_api_text = json.loads((Path(job.output_root) / 'translations.json').read_text(encoding='utf-8'))
    assert saved_api_text[units[0]['id']] == '错误姓名“你好。”'
    translate(job, corpus, lambda: pytest.fail('Resume must not call API'), workers=1)
    assert (game / 'pac/ysbin.ypf').read_bytes() == source


def test_qlie_extract_translate_and_publish_uses_same_automatic_names(tmp_path, monkeypatch):
    game, export, corpus = tmp_path / 'game', tmp_path / 'export', tmp_path / 'corpus'
    game.mkdir()
    original = '〖六花〗\r\n「こんにちは。」\r\n〖六花〗\r\n「また会いましたね。」\r\n'
    write_export_workspace(export, [{'output_path': 'scenario/main.s', 'data': original.encode('utf-8')}])
    plan = build_qlie_corpus_plan(export, corpus)
    assert plan.status == 'ready'
    assert apply_qlie_corpus_plan(plan).status == 'published'
    catalog = load_qlie_characters(corpus)
    assert len(catalog['characters']) == 1
    calls = []
    def respond(messages, **kwargs):
        if not messages[1]['content'].startswith('REQUEST_JSON:'):
            calls.append('names')
            rows = json.loads(messages[1]['content'])['characters']
            return json.dumps({'translations': [{'id': c['id'], 'text': '六华'} for c in rows]})
        calls.append('body')
        assert calls[0] == 'names'
        payload = json.loads(messages[1]['content'].split('REQUEST_JSON:', 1)[1])
        assert any(t['source'] == '六花' and t['target'] == '六华' for t in payload['terminology'])
        return json.dumps({'schema_version': 1, 'request_id': payload['request_id'],
                           'translations': [{'unit_id': u['unit_id'], 'segment_id': u['segment_id'],
                                             'translated_text': '〖错误译名〗' if u['kind'] == 'speaker_name' else '「你好。」'}
                                            for u in payload['units']]})
    monkeypatch.setattr(webapp, '_model_client', lambda *a: SimpleNamespace(complete=respond))
    monkeypatch.setattr(webapp, '_completed_translation_run', lambda *a: None)
    monkeypatch.setattr(webapp, '_project_root', lambda: tmp_path)
    monkeypatch.setattr(webapp, 'local_models', lambda: (None, None, ()))
    job = job_at(tmp_path)
    job.context_config = TranslationContextConfig(rag_budget_chars=0)
    webapp._translate_and_publish(job, export, corpus)
    assert job.status == 'completed' and calls.count('names') == 1
    output = Path(job.translated_scripts_dir) / 'scenario/main.s'
    assert output.read_text(encoding='utf-8').count('〖六华〗') == 2
    assert (export / 'scenario/main.s').read_bytes() == original.encode('utf-8')
    glossary = load_glossary(Path(job.output_root) / GLOSSARY_FILE)
    new_plan, requests, snapshot = prepare_translation_requests(corpus, model_id='fake', character_glossary=glossary,
                                                               config=TranslationContextConfig(rag_budget_chars=0))
    legacy_plan, _, _ = prepare_translation_requests(corpus, model_id='fake')
    assert snapshot.version == glossary.version
    assert new_plan.batches[0].units[0].cache_key != legacy_plan.batches[0].units[0].cache_key
    assert requests[0].terminology  # Mandatory names survive a zero RAG budget.
    next_job = job_at(tmp_path, output='partial-b', mode='partial')
    next_job.context_config = TranslationContextConfig(rag_budget_chars=0)
    webapp._translate_and_publish(next_job, export, corpus)
    assert next_job.status == 'completed' and calls.count('names') == 1
    assert next_job.total_units == 2  # Speaker-name fields do not consume story slots.
    assert (Path(next_job.translated_scripts_dir) / 'scenario/main.s').read_text(encoding='utf-8').count('〖六华〗') == 2
    saved_selection = json.loads((Path(next_job.output_root) / 'opening-selection.json').read_text(encoding='utf-8'))
    assert len(saved_selection['selected_ids']) == len(saved_selection['display_ids']) == 2


def test_yuris_legacy_paid_batches_still_resume_without_name_calls(tmp_path):
    source = speaker_archive()
    job = job_at(tmp_path)
    game = Path(job.game_dir)
    (game / 'pac').mkdir(parents=True)
    (game / 'game.exe').write_bytes(b'MZ synthetic YU-RIS executable')
    (game / 'pac/ysbin.ypf').write_bytes(source)
    _, corpus = yuris.extract_game(game, tmp_path / 'extracted', job)
    units = json.loads((corpus / 'texts.json').read_text(encoding='utf-8'))
    planned = list(batches(units))
    from nagi.translation.yuris import PROMPT
    identity = {'engine': yuris.ENGINE, 'provider': 'fake', 'model': 'fake', 'archive_sha256': yuris.sha(source),
                'prompt_sha256': yuris.sha(PROMPT.encode()),
                'units_sha256': yuris.sha(json.dumps(units, ensure_ascii=False, sort_keys=True).encode()),
                'text_count': len(units), 'batch_count': len(planned), 'content_filter': False,
                'translation_quality_check': False}
    root = Path(job.output_root)
    yuris.save_json(root / 'translation-plan.json', identity)
    for index, batch in enumerate(planned):
        raw = json.dumps({'translations': batch})
        yuris.save_json(root / f'api-batches/{index:05d}.response.json', {
            'response': raw, 'input_ids': [u['id'] for u in batch],
            'request_sha256': yuris.sha(json.dumps(batch_messages(batch), ensure_ascii=False).encode())})
    translate(job, corpus, lambda: pytest.fail('Legacy paid task must not acquire names'), workers=1)
    assert job.status == 'completed' and not (root / GLOSSARY_FILE).exists()
