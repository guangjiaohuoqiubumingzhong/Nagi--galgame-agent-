"""Read-only engine adapters for the shared character catalogue contract."""

import hashlib
import json
from pathlib import Path

CATALOG_FILE = "characters.json"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def character(engine, lookup, display):
    for text in (lookup, display):
        if not isinstance(text, str) or not text.strip() or len(text) > 256 or any(ord(c) < 32 for c in text):
            raise ValueError("人物登记名无效或过长，未调用翻译 API")
    return {"id": "char_" + fingerprint([engine, lookup]), "lookup_name": lookup,
            "source_name": display, "aliases": sorted({lookup, display})}


def catalogue(engine, source_sha256, characters, bindings):
    payload = {"schema_version": 1, "engine": engine, "source_sha256": source_sha256,
               "characters": sorted(characters, key=lambda c: c["id"]),
               "bindings": bindings}
    return {**payload, "catalog_sha256": fingerprint(payload)}


def publish_catalogue(directory, payload):
    from .yuris import save_json

    path = Path(directory) / CATALOG_FILE
    if path.is_symlink():
        raise ValueError("人物登记表不能使用符号链接")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("人物登记表与原始脚本不一致，请重新提取")
    else:
        save_json(path, payload)
    return payload


def yuris_characters(entries, table, key, units, source_sha256):
    from .yuris import literal, parse_script

    characters, bindings, marks = {}, {}, set()
    unit_ids = {u["id"] for u in units}
    for entry in entries:
        if entry["content"][:4] != b"YSTB":
            continue
        for ins in parse_script(entry["content"], key, table)[0]:
            if ins["command"] != "GOSUB":
                continue
            fields = {ins["parameters"][f["id"]]: f for f in ins["fields"]
                      if f["id"] < len(ins["parameters"]) and f["type"] == 3}
            values = {name: literal(f["blob"] or b"") for name, f in fields.items()}
            routine = (values.get("#") or "").upper()
            if routine == "ES.CHAR.NAME.MARK.SET" and values.get("PSTR"):
                marks.add(values["PSTR"])
            if routine != "ES.CHAR.NAME" or not values.get("PSTR"):
                continue
            lookup = values["PSTR"]
            record = character("YU-RIS", lookup, values.get("PSTR2") or lookup)
            if lookup in characters and characters[lookup] != record:
                raise ValueError("同一人物识别名有不同显示名，当前适配器无法无歧义绑定")
            characters[lookup] = record
            for parameter, role in (("PSTR", "lookup"), ("PSTR2", "display")):
                if parameter in fields:
                    identifier = f"{entry['name']}:{fields[parameter]['argument']}"
                    if identifier in unit_ids:
                        bindings[identifier] = {"character_id": record["id"], "role": role}
    for unit in units:
        if unit["command"] != "WORD" or unit["representation"] != "raw":
            continue
        match = next(((name, mark) for name in sorted(characters, key=lambda s: (-len(s), s))
                      for mark in sorted(marks, key=lambda s: (-len(s), s))
                      if unit["text"].startswith(name + mark)), None)
        if match:
            name, mark = match
            bindings[unit["id"]] = {"character_id": characters[name]["id"],
                                    "role": "dialogue", "name_mark": mark}
    return catalogue("YU-RIS", source_sha256, characters.values(), bindings)


def qlie_characters(segments, source_sha256):
    characters, bindings = {}, {}
    for segment in segments:
        if not segment.speaker:
            continue
        record = character("QLIE", segment.speaker, segment.speaker)
        characters[record["id"]] = record
        if segment.kind in {"speaker_name", "dialogue"}:
            bindings[segment.segment_id] = {"character_id": record["id"],
                                           "role": "display" if segment.kind == "speaker_name" else "dialogue"}
    return catalogue("QLIE", source_sha256, characters.values(), bindings)


def load_qlie_characters(corpus):
    from .segments import TextSegment

    raw = (Path(corpus) / "segments.jsonl").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    report = json.loads((Path(corpus) / "parse-report.json").read_text(encoding="utf-8"))
    if report["artifacts"]["segments"]["sha256"] != digest:
        raise ValueError("语料内容已改变，未调用人物名翻译 API")
    segments = [TextSegment.from_dict(json.loads(line)) for line in raw.splitlines()]
    return publish_catalogue(corpus, qlie_characters(segments, digest))
