"""YU-RIS 479 archive/text adapter. No content or translation-quality filtering.

Format references: YU-RIS documentation and VNTranslationTools' Yuris notes.
Only string payloads are replaced; opcodes, variable IDs and jumps stay intact.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from collections import Counter
from pathlib import Path, PureWindowsPath
from uuid import uuid4

ENGINE = "yuris-479"
NAME_MAP = bytearray(range(256))
for _a, _b in (
    (3, 72),
    (17, 25),
    (46, 50),
    (6, 53),
    (9, 11),
    (12, 16),
    (13, 19),
    (21, 27),
    (28, 30),
    (32, 35),
    (38, 41),
    (44, 47),
):
    NAME_MAP[_a], NAME_MAP[_b] = NAME_MAP[_b], NAME_MAP[_a]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def murmur2(data):
    mask, multiplier = 0xFFFFFFFF, 0x5BD1E995
    value = len(data)
    boundary = len(data) // 4 * 4
    for offset in range(0, boundary, 4):
        word = struct.unpack_from("<I", data, offset)[0]
        word = word * multiplier & mask
        word ^= word >> 24
        value = (value * multiplier & mask) ^ (word * multiplier & mask)
    tail = data[boundary:]
    if tail:
        value ^= int.from_bytes(tail, "little")
        value = value * multiplier & mask
    value ^= value >> 13
    value = value * multiplier & mask
    return value ^ (value >> 15)


def archive_entries(data):
    magic, version, count, header = struct.unpack_from("<4sIII", data)
    if (
        magic != b"YPF\0"
        or version != 479
        or count > 100000
        or not 32 <= header <= len(data)
    ):
        raise ValueError("当前 YU-RIS 适配要求有效的 YPF 479 脚本包")
    entries, names, offset = [], set(), 32
    for _ in range(count):
        checksum, length = struct.unpack_from("<IB", data, offset)
        offset += 5
        length = NAME_MAP[length ^ 255]
        raw_name = bytes(value ^ 255 for value in data[offset : offset + length])
        offset += length
        name = raw_name.decode("cp932")
        parsed = PureWindowsPath(name)
        if (
            parsed.is_absolute()
            or parsed.drive
            or ".." in parsed.parts
            or ":" in name
            or not name
        ):
            raise ValueError("YPF contains an unsafe filename")
        canonical = "/".join(parsed.parts)
        if canonical.casefold() in names or murmur2(raw_name) != checksum:
            raise ValueError("YPF filename checksum/uniqueness failure")
        names.add(canonical.casefold())
        kind, compressed, size, packed_size, position, data_hash = struct.unpack_from(
            "<BBIIQI", data, offset
        )
        offset += 22
        if (
            compressed not in (0, 1)
            or size > 64 * 1024 * 1024
            or position < header
            or position + packed_size > len(data)
        ):
            raise ValueError("YPF entry bounds are invalid")
        packed = data[position : position + packed_size]
        if murmur2(packed) != data_hash:
            raise ValueError("YPF entry checksum failure")
        if compressed:
            decoder = zlib.decompressobj()
            content = decoder.decompress(packed, size + 1)
            if not decoder.eof or decoder.unused_data:
                raise ValueError("Invalid YPF compressed stream")
        else:
            content = packed
        if len(content) != size:
            raise ValueError("YPF entry size mismatch")
        entries.append(
            {
                "name": canonical,
                "raw_name": raw_name,
                "kind": kind,
                "compressed": compressed,
                "content": content,
            }
        )
    if offset != header:
        raise ValueError("YPF table length mismatch")
    return entries


def pack_archive(entries):
    header = 32 + sum(27 + len(item["raw_name"]) for item in entries)
    table, body = bytearray(), bytearray()
    for item in entries:
        raw, content = item["raw_name"], item["content"]
        packed = zlib.compress(content) if item["compressed"] else content
        table.extend(struct.pack("<IB", murmur2(raw), NAME_MAP[len(raw)] ^ 255))
        table.extend(value ^ 255 for value in raw)
        table.extend(
            struct.pack(
                "<BBIIQI",
                item["kind"],
                item["compressed"],
                len(content),
                len(packed),
                header + len(body),
                murmur2(packed),
            )
        )
        body.extend(packed)
    return struct.pack("<4sIII16x", b"YPF\0", 479, len(entries), header) + table + body


def commands(data):
    magic, version, count, reserved = struct.unpack_from("<4sIII", data)
    if magic != b"YSCM" or version != 479 or reserved:
        raise ValueError("Unsupported YU-RIS command table")
    offset, result = 16, []
    for _ in range(count):
        end = data.index(0, offset)
        name = data[offset:end].decode("cp932")
        parameter_count = data[end + 1]
        offset = end + 2
        parameters = []
        for _ in range(parameter_count):
            end = data.index(0, offset)
            parameters.append(data[offset:end].decode("cp932"))
            offset = end + 3
        result.append((name, parameters))
    return result


def crypt(data, key):
    return bytes(value ^ key[index % 4] for index, value in enumerate(data))


def parse_script(data, key, table):
    magic, version, count, code_size, arg_size, res_size, line_size, reserved = (
        struct.unpack_from("<4s7I", data)
    )
    if (
        magic != b"YSTB"
        or version != 479
        or code_size != count * 4
        or arg_size % 12
        or line_size != count * 4
        or reserved
        or len(data) != 32 + code_size + arg_size + res_size + line_size
    ):
        raise ValueError("Invalid YSTB 479 section layout")
    offset, sections = 32, []
    for size in (code_size, arg_size, res_size, line_size):
        sections.append(crypt(data[offset : offset + size], key))
        offset += size
    code, arguments, resources, _ = sections
    result, arg_index = [], 0
    for instruction, (opcode, count, _npar) in enumerate(
        struct.iter_unpack("<BBH", code)
    ):
        if opcode >= len(table) or (arg_index + count) * 12 > arg_size:
            raise ValueError("Invalid YU-RIS opcode or encryption key")
        fields = []
        for index in range(arg_index, arg_index + count):
            ident, typ, assign, size, pos = struct.unpack_from(
                "<HBBII", arguments, index * 12
            )
            fields.append(
                {
                    "argument": index,
                    "id": ident,
                    "type": typ,
                    "assign": assign,
                    "size": size,
                    "position": pos,
                    "blob": resources[pos : pos + size]
                    if pos + size <= len(resources)
                    else None,
                }
            )
        result.append(
            {
                "instruction": instruction,
                "command": table[opcode][0],
                "parameters": table[opcode][1],
                "fields": fields,
            }
        )
        arg_index += count
    if arg_index * 12 != arg_size:
        raise ValueError("YU-RIS argument count mismatch")
    return result, sections


def find_key(entries, table):
    # Every script has its own zero-based resource section. The first argument's
    # encrypted resource offset exposes the four-byte XOR key (verify by parsing).
    for item in entries:
        data = item["content"]
        if data[:4] != b"YSTB" or len(data) < 44:
            continue
        code_size, arg_size = struct.unpack_from("<II", data, 12)
        if arg_size:
            key = data[32 + code_size + 8 : 32 + code_size + 12]
            try:
                parse_script(data, key, table)
                return key
            except (ValueError, struct.error):
                continue
    raise ValueError("Cannot determine YU-RIS script encryption key")


def literal(blob):
    if (
        not blob
        or len(blob) < 5
        or blob[0] != 0x4D
        or struct.unpack_from("<H", blob, 1)[0] != len(blob) - 3
    ):
        return None
    text = blob[3:].decode("cp932")
    if len(text) < 2 or text[0] != text[-1]:
        return None
    return re.sub(
        r"\\([\\nt])",
        lambda match: {"\\": "\\", "n": "\n", "t": "\t"}[match[1]],
        text[1:-1],
    )


def expression_literals(blob):
    """Locate PushString operands without interpreting/changing other bytecode."""
    result, offset = [], 0
    while offset < len(blob):
        if offset + 3 > len(blob):
            raise ValueError("Truncated YU-RIS expression instruction")
        opcode, size = struct.unpack_from("<BH", blob, offset)
        end = offset + 3 + size
        if end > len(blob):
            raise ValueError("Truncated YU-RIS expression operand")
        if opcode == 0x4D:
            text = literal(blob[offset:end])
            if text is None:
                raise ValueError("Invalid YU-RIS string operand")
            result.append((offset, end, text))
        offset = end
    return result


def display_literal(command, text):
    # Distinguish display text from resource names/technical symbols, not from
    # subject matter. Raw WORD dialogue never passes through this selection.
    if not text:
        return False
    if command in ("LET", "STR", "GOSUB") and not re.search(
        r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9d]", text
    ):
        return False
    return not (
        command in ("LET", "STR")
        and re.search(
            r"\.(?:ogg|wav|png|bmp|jpg|ybn|ypf|txt|dat)(?:$|[\"'])", text, re.IGNORECASE
        )
    )


TEXT_SUBROUTINES = {
    "ES.CHAR.NAME",
    "ES.SEL.SET",
    "ES._MES",
    "ES.DIALOG.YESNO.SET",
    "ES.DIALOG.SET",
    "ES.BT.NAME.SET",
    "ES.DATE.WEEK.SET",
    "ES.MAKELOG",
    "ES.MAKELOG2",
    "ES.DATE",
}
TEXT_PARAMETERS = {
    "MENU": {"NAME"},
    "DIALOG": {"CAPTION", "STR"},
    "WINDOW": {"CAPTION"},
    "CGACT": {"SETSTR"},
}


def text_fields(instructions):
    for instruction in instructions:
        fields, command = instruction["fields"], instruction["command"]
        subroutine = (
            literal(fields[0]["blob"]) if command == "GOSUB" and fields else None
        )
        for field in fields:
            blob = field["blob"]
            if blob is None:
                continue
            if command == "WORD" and field["type"] == 0:
                text, representation = blob.decode("cp932"), "raw"
            else:
                if field["type"] != 3:
                    continue
                parameter = (
                    instruction["parameters"][field["id"]]
                    if field["id"] < len(instruction["parameters"])
                    else ""
                )
                selected = (
                    command == "_"
                    or command in ("LET", "STR")
                    or (
                        command == "GOSUB"
                        and subroutine
                        and subroutine.upper() in TEXT_SUBROUTINES
                        and parameter.startswith("PSTR")
                    )
                    or parameter in TEXT_PARAMETERS.get(command, set())
                )
                if not selected:
                    continue
                text, representation = literal(blob), "literal"
                if text is None:
                    for start, end, part in expression_literals(blob):
                        if display_literal(command, part):
                            fragment = {
                                **field,
                                "expression_offset": start,
                                "expression_end": end,
                            }
                            yield instruction, fragment, part, "expression"
                    continue
                if not display_literal(command, text):
                    continue
            if text:
                yield instruction, field, text, representation


def catalog(entries):
    table = commands(
        next(
            item["content"]
            for item in entries
            if item["name"].endswith("/ysc.ybn") or item["name"] == "ysc.ybn"
        )
    )
    key, units = find_key(entries, table), []
    for item in entries:
        if item["content"][:4] != b"YSTB":
            continue
        instructions, _ = parse_script(item["content"], key, table)
        for instruction, field, text, representation in text_fields(instructions):
            identity = f"{item['name']}:{field['argument']}"
            fragment = {}
            if representation == "expression":
                identity += f":expr:{field['expression_offset']}"
                fragment = {
                    name: field[name]
                    for name in ("expression_offset", "expression_end")
                }
            units.append(
                {
                    "id": identity,
                    "script": item["name"],
                    "instruction": instruction["instruction"],
                    "argument": field["argument"],
                    "command": instruction["command"],
                    "representation": representation,
                    "text": text,
                    **fragment,
                }
            )
    return table, key, units


def extract_game(game, output, job):
    from .characters import publish_catalogue, yuris_characters

    archive = Path(game) / "pac" / "ysbin.ypf"
    data = archive.read_bytes()
    entries = archive_entries(data)
    table, key, units = catalog(entries)
    output = Path(output)
    corpus = output / "source"
    if job.cancel_event.is_set():
        raise InterruptedError()
    corpus.mkdir(parents=True, exist_ok=True)
    save_json(corpus / "texts.json", units)
    characters = publish_catalogue(corpus, yuris_characters(entries, table, key, units, sha(data)))
    save_json(
        corpus / "extraction.json",
        {
            "engine": ENGINE,
            "source_archive": "pac/ysbin.ypf",
            "storage_format": "text-only-v1",
            "archive_sha256": sha(data),
            "key_hex": key.hex(),
            "file_count": len(entries),
            "text_count": len(units),
            "character_count": len(characters["characters"]),
            "commands": dict(Counter(unit["command"] for unit in units)),
            "content_filter": False,
        },
    )
    job.update(
        export_dir=str(corpus),
        corpus_dir=str(corpus),
        total_units=len(units),
        progress=0.34,
        character_count=len(characters["characters"]),
        event=f"YU-RIS 提取完成：{len(entries)} 个文件、{len(units)} 条文本；未做内容筛选",
    )
    return corpus, corpus


def load_source(corpus, game_dir):
    """Keep originals read-only instead of retaining binary extraction copies."""
    corpus = Path(corpus)
    extraction = json.loads((corpus / "extraction.json").read_text(encoding="utf-8"))
    if extraction.get("storage_format") == "text-only-v1":
        game = Path(game_dir).resolve()
        archive = (game / extraction["source_archive"]).resolve()
        if game not in archive.parents:
            raise ValueError("Source archive escapes the original game")
    else:
        archive = corpus / "source.ypf"  # Backward-compatible saved workflows.
    source = archive.read_bytes()
    if sha(source) != extraction["archive_sha256"]:
        raise ValueError("原始游戏脚本包发生变化，请恢复原文件或重新提取，未调用 API")
    return source, extraction


def is_yuris(game):
    path = Path(game) / "pac" / "ysbin.ypf"
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) == b"YPF\0"


def glyph_map(entries, translations):
    text = "".join(translations.values())
    occupied = set(text)
    table, key, _ = catalog(entries)
    for item in entries:
        content = item["content"]
        # YSTB resources are encrypted on disk. Reserve the original decoded
        # glyphs too, including text/labels that are not translation targets.
        if content[:4] == b"YSTB":
            content = parse_script(content, key, table)[1][2]
        occupied.update(content.decode("cp932", errors="ignore"))
    missing = set()
    for character in set(text):
        try:
            if character.encode("cp932").decode("cp932") != character:
                missing.add(character)
        except UnicodeEncodeError:
            missing.add(character)
    available = []
    # Prefer user-defined CP932 glyph slots, then unused kanji if a full game
    # exceeds the pilot's 1880-character private-use capacity.
    for codepoint in (*range(0xE000, 0xE758), *range(0x3400, 0xA000)):
        character = chr(codepoint)
        if character in occupied:
            continue
        try:
            encoded = character.encode("cp932")
        except UnicodeEncodeError:
            continue
        if (
            len(encoded) == 2
            and encoded[0] != 0xEF
            and encoded.decode("cp932") == character
        ):
            available.append(character)
    if len(missing) > len(available):
        raise ValueError(
            "Chinese glyph mapping capacity exceeded; no lossy replacement was made"
        )
    return dict(zip(sorted(missing), available))


def patched_archive(source, translations, *, selected_ids=None, character_glossary=None):
    from .yuris_speakers import preserve_speaker_bindings

    entries = archive_entries(source)
    table, key, units = catalog(entries)
    catalog_ids = {unit["id"] for unit in units}
    expected = catalog_ids if selected_ids is None else set(selected_ids)
    if not expected or not expected <= catalog_ids or set(translations) != expected:
        raise ValueError(
            "Translation result IDs do not cover the complete selected extracted catalog"
        )
    if character_glossary is not None:
        from .characters import yuris_characters

        actual = yuris_characters(entries, table, key, units, sha(source))
        if character_glossary.catalog != actual:
            raise ValueError("人物译名表与游戏脚本不一致，停止回填")
        # Names are a separate, explicitly translated scope even in a 50-line run.
        translations = dict(translations)
        for identifier in actual["bindings"]:
            target = character_glossary.display_translation(identifier)
            if target is not None:
                translations[identifier] = target
                expected.add(identifier)
    translations = preserve_speaker_bindings(entries, table, key, units, translations)
    encoding = glyph_map(entries, translations)
    by_script = {}
    for unit in units:
        if unit["id"] in expected:
            by_script.setdefault(unit["script"], []).append(unit)
    for entry in entries:
        if entry["name"] not in by_script:
            continue
        original = entry["content"]
        _, (code, args, resources, lines) = parse_script(original, key, table)
        args_out, res_out = bytearray(args), bytearray(resources)
        replacements = {}
        for unit in by_script[entry["name"]]:
            text = "".join(encoding.get(c, c) for c in translations[unit["id"]])
            if unit["representation"] in ("literal", "expression"):
                delimiter = next(
                    (d for d in ('"', "'", "`", "~") if d not in text), None
                )
                if delimiter is None:
                    raise ValueError("Cannot delimit YU-RIS string literal")
                text = (
                    delimiter
                    + text.replace("\\", "\\\\")
                    .replace("\n", "\\n")
                    .replace("\t", "\\t")
                    + delimiter
                )
                payload = text.encode("cp932")
                payload = struct.pack("<BH", 0x4D, len(payload)) + payload
            else:
                payload = text.replace("\r\n", "\n").replace("\n", " ").encode("cp932")
            if unit["representation"] == "expression":
                span = (unit["expression_offset"], unit["expression_end"], payload)
            else:
                old_size = struct.unpack_from("<I", args, unit["argument"] * 12 + 4)[0]
                span = (0, old_size, payload)
            replacements.setdefault(unit["argument"], []).append(span)
        for argument, spans in replacements.items():
            old_size, old_pos = struct.unpack_from("<II", args, argument * 12 + 4)
            old_blob = resources[old_pos : old_pos + old_size]
            cursor, parts = 0, []
            for start, end, replacement in sorted(spans):
                if not cursor <= start < end <= old_size:
                    raise ValueError("Overlapping YU-RIS expression replacements")
                parts.extend((old_blob[cursor:start], replacement))
                cursor = end
            parts.append(old_blob[cursor:])
            payload = b"".join(parts)
            struct.pack_into(
                "<II", args_out, argument * 12 + 4, len(payload), len(res_out)
            )
            res_out.extend(payload)
        header = bytearray(original[:32])
        struct.pack_into("<I", header, 20, len(res_out))
        entry["content"] = bytes(header) + b"".join(
            crypt(part, key) for part in (code, args_out, res_out, lines)
        )
        parse_script(entry["content"], key, table)
    packed = bytes(pack_archive(entries))
    # Binary roundtrip is deployment integrity, not semantic translation grading.
    unpacked = archive_entries(packed)
    if [(item["name"], sha(item["content"])) for item in unpacked] != [
        (item["name"], sha(item["content"])) for item in entries
    ]:
        raise ValueError("Repacked YPF does not reproduce the patched scripts")
    return packed, {value: key for key, value in encoding.items()}
