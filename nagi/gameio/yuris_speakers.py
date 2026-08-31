"""Preserve YU-RIS registered speaker bindings when inserting API text.

ES.CHAR.NAME's PSTR is an engine lookup key; PSTR2 is the displayed name.
WORD uses that key followed by a registered name mark. Translating each string
independently must not change the key or turn the mark into Chinese quotes.
This is deployment structure repair, not a rewrite of the saved API response.
"""

import re

from .yuris import literal, parse_script

_CLOSERS = {"「": "」", "『": "』", "（": "）", "“": "”", "‘": "’", '"': '"', "(": ")"}


def speaker_bindings(entries, table, key):
    names, marks, key_fields = set(), set(), {}
    for entry in entries:
        if entry["content"][:4] != b"YSTB":
            continue
        instructions, _ = parse_script(entry["content"], key, table)
        for instruction in instructions:
            if instruction["command"] != "GOSUB":
                continue
            fields = {}
            for field in instruction["fields"]:
                if field["id"] < len(instruction["parameters"]):
                    fields[instruction["parameters"][field["id"]]] = field
            routine, name_field = fields.get("#"), fields.get("PSTR")
            if not routine or not name_field or routine["type"] != 3 or name_field["type"] != 3:
                continue
            subroutine = literal(routine["blob"] or b"")
            name = literal(name_field["blob"] or b"")
            if not subroutine or not name:
                continue
            if subroutine.upper() == "ES.CHAR.NAME":
                names.add(name)
                key_fields[f"{entry['name']}:{name_field['argument']}"] = name
            elif subroutine.upper() == "ES.CHAR.NAME.MARK.SET":
                marks.add(name)
    return names, marks, key_fields


def restore_dialogue_prefix(source, translated, names, marks):
    binding = next(
        ((name, mark) for name in sorted(names, key=lambda n: (-len(n), n))
         for mark in sorted(marks, key=lambda m: (-len(m), m))
         if source.startswith(name + mark)), None,
    )
    if binding is None:
        return translated
    name, mark = binding
    # Keep unmodified input byte-for-byte, including any engine control tokens.
    if translated == source:
        return translated
    if mark not in _CLOSERS:
        if translated.startswith(name + mark):
            return translated
        raise ValueError("人物名使用了尚未支持的分隔符，停止回填以免姓名进入正文")
    opening = re.search(r'[「『（“‘"(]', translated)
    if opening is None:
        raise ValueError("无法识别人物名与对话的分隔符，停止回填以免丢失正文")
    prefix = translated[:opening.start()]
    if len(prefix) > 32 or re.search(r'[\r\n，。！？、,!?;；]', prefix):
        raise ValueError("人物名前缀不明确，停止回填以免丢失正文")
    body = translated[opening.end():]
    closer = _CLOSERS[opening[0]]
    trimmed = body.rstrip()
    if trimmed.endswith(closer):
        body = trimmed[:-len(closer)] + _CLOSERS[mark] + body[len(trimmed):]
    return name + mark + body


def preserve_speaker_bindings(entries, table, key, units, translations):
    """Return a new mapping; keep catalog IDs and paid API receipts unchanged."""
    names, marks, key_fields = speaker_bindings(entries, table, key)
    result = dict(translations)
    for unit in units:
        identifier = unit["id"]
        if identifier not in result:
            continue
        if identifier in key_fields:
            # The separately translated PSTR2 remains the visible name label.
            result[identifier] = key_fields[identifier]
        elif unit["command"] == "WORD" and unit["representation"] == "raw":
            try:
                result[identifier] = restore_dialogue_prefix(
                    unit["text"], result[identifier], names, marks,
                )
            except ValueError as exc:
                raise ValueError(f"{identifier}: {exc}") from exc
    return result
