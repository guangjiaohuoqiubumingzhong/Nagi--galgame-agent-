import struct

import pytest

from nagi.gameio import yuris
from nagi.gameio.yuris_speakers import restore_dialogue_prefix, speaker_bindings


def speaker_archive(extra_dialogue=0, registry_last=False):
    parameters = [('#', 'PSTR', 'PSTR2'), ('STR',)]
    table = b'YSCM' + struct.pack('<III', 479, 2, 0)
    for command, fields in zip(('GOSUB', 'WORD'), parameters):
        table += command.encode() + b'\0' + bytes([len(fields)])
        for field in fields:
            table += field.encode() + b'\0\0\0'
    instructions = [
        (0, ['es.CHAR.NAME.MARK.SET', '「']),
        (0, ['es.CHAR.NAME.MARK.SET', '『']),
        (0, ['es.CHAR.NAME', '一房', '一房']),
        (0, ['es.CHAR.NAME', '女子１', '女子１']),
        (1, ['一房「これは台詞です。」']),
        (1, ['女子１『これは台詞です。』']),
        (1, ['わたしは「一房」と呼んだ。']),
    ]
    instructions.extend((1, [f'一房「追加の台詞{i}です。」']) for i in range(extra_dialogue))
    if registry_last:
        instructions = instructions[4:] + instructions[:4]
    code, args, resources = bytearray(), bytearray(), bytearray()
    for opcode, fields in instructions:
        code += struct.pack('<BBH', opcode, len(fields), 0)
        for index, value in enumerate(fields):
            blob = value.encode('cp932')
            if opcode == 0:
                blob = b'"' + blob + b'"'
                blob = struct.pack('<BH', 0x4D, len(blob)) + blob
            args += struct.pack('<HBBII', index, 3 if opcode == 0 else 0, 0, len(blob), len(resources))
            resources += blob
    key = bytes.fromhex('d36fac96')
    lines = bytes(len(code))
    script = struct.pack('<4s7I', b'YSTB', 479, len(instructions), len(code), len(args), len(resources), len(lines), 0)
    script += b''.join(yuris.crypt(part, key) for part in (code, args, resources, lines))
    entries = [
        {'name': name, 'raw_name': name.encode(), 'kind': 0, 'compressed': 1, 'content': content}
        for name, content in [('ysc.ybn', table), ('scene.ybn', script)]
    ]
    return bytes(yuris.pack_archive(entries))


@pytest.mark.parametrize(('translated', 'expected'), [
    ('和房「对不起。」', '一房「对不起。」'),
    ('一房：“对不起。”', '一房「对不起。」'),
    ('“对不起。”', '一房「对不起。」'),
    ('Kazufusa: "对不起。"', '一房「对不起。」'),
    ('一房「他说“你好”。」', '一房「他说“你好”。」'),
])
def test_restore_engine_name_and_marks(translated, expected):
    assert restore_dialogue_prefix('一房「すみません。」', translated, {'一房'}, {'「'}) == expected


def test_only_registered_speakers_are_repaired():
    for source in ('わたしは「一房」と呼んだ。', '「話者なし」', '未登録「台詞」'):
        assert restore_dialogue_prefix(source, '这是「引用」文本。', {'一房'}, {'「'}) == '这是「引用」文本。'
    assert restore_dialogue_prefix('一房『台詞』', '和房“译文”', {'一房'}, {'『'}) == '一房『译文』'
    assert restore_dialogue_prefix('一房（台詞）', '和房(译文)', {'一房'}, {'（'}) == '一房（译文）'


@pytest.mark.parametrize('translated', ['没有分隔符', '一房说了一句话。然后「你好」', '姓名\n「你好」'])
def test_ambiguous_boundary_fails_without_dropping_text(translated):
    with pytest.raises(ValueError, match='停止回填'):
        restore_dialogue_prefix('一房「台詞」', translated, {'一房'}, {'「'})


@pytest.mark.parametrize('partial', [False, True])
def test_patch_keeps_lookup_key_separate_from_display_name(partial):
    source = speaker_archive()
    entries = yuris.archive_entries(source)
    table, key, units = yuris.catalog(entries)
    names, marks, keys = speaker_bindings(entries, table, key)
    assert names == {'一房', '女子１'} and marks == {'「', '『'}
    dialogue = [u for u in units if u['command'] == 'WORD']
    translations = {u['id']: u['text'] for u in units}
    translations.update({identifier: '不应作为识别键' for identifier in keys})
    # Display-name strings are intentionally different from engine lookup keys.
    display = next(u for u in units if u['text'] == '女子１' and u['id'] not in keys)
    translations[display['id']] = '女生1'
    translations[dialogue[0]['id']] = '和房“你好。”'
    translations[dialogue[1]['id']] = '女生1：“早上好。”'
    if partial:
        translations = {dialogue[0]['id']: translations[dialogue[0]['id']]}
    snapshot = dict(translations)
    packed, glyphs = yuris.patched_archive(source, translations, selected_ids=set(translations) if partial else None)
    assert translations == snapshot  # Paid responses are never rewritten.
    updated = yuris.archive_entries(packed)
    actual = {u['id']: ''.join(glyphs.get(c, c) for c in u['text']) for u in yuris.catalog(updated)[2]}
    assert actual[dialogue[0]['id']] == '一房「你好。」'
    assert actual[dialogue[1]['id']] == (dialogue[1]['text'] if partial else '女子１『早上好。』')
    assert actual[display['id']] == ('女子１' if partial else '女生1')
    for identifier, name in keys.items():
        assert actual[identifier] == name
    assert actual[dialogue[2]['id']] == dialogue[2]['text']
    old = yuris.parse_script(entries[1]['content'], key, table)[1]
    new = yuris.parse_script(updated[1]['content'], key, table)[1]
    assert old[0] == new[0] and old[3] == new[3]
    # Partial translation leaves all nonselected instruction arguments untouched.
    if partial:
        changed = dialogue[0]['argument']
        for index in range(len(old[1]) // 12):
            if index != changed:
                assert old[1][index*12:(index+1)*12] == new[1][index*12:(index+1)*12]
