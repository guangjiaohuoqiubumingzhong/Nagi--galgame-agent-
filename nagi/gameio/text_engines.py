"""Adapters for text-script visual novel engines.

The adapters deliberately fail closed on encrypted/compiled-only releases.  They
share one manifest and patch contract so the public extract/translate/deploy
workflow stays engine independent.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..translation_storage import deployment_paths, publish_playable

XP3_SIGNATURE = b"XP3\r\n \n\x1a\x8bg\x01"
SOURCE_KINDS = {
    "kirikiri": "KiriKiri",
    "renpy": "RenPy",
    "tyranoscript": "TyranoScript",
}
TOKEN_RE = re.compile(r"\[[^\]\r\n]*\]|\{[^{}\r\n]*\}")
RENPY_NON_DIALOGUE = {
    "call", "camera", "define", "default", "hide", "image", "jump", "label",
    "play", "python", "queue", "scene", "screen", "show", "stop", "style",
    "transform", "voice", "window", "with",
}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_probe(path, limit=2 * 1024 * 1024):
    with Path(path).open("rb") as stream:
        return stream.read(limit)


def _executables(game):
    ignored = {"crashreporter.exe", "unins000.exe", "uninstall.exe", "dxsetup.exe"}
    return sorted(
        (path for path in Path(game).glob("*.exe") if path.name.lower() not in ignored),
        key=lambda path: (path.name.casefold() != (Path(game).name + ".exe").casefold(), path.name.casefold()),
    )


def detect_engine(game):
    """Return a supported source kind only when positive engine evidence exists."""
    game = Path(game)
    if (game / "tyrano/tyrano.base.js").is_file() or (
        (game / "data/scenario").is_dir()
        and ((game / "index.html").is_file() or (game / "package.json").is_file())
    ):
        return "tyranoscript"
    renpy_game = game / "game"
    if renpy_game.is_dir() and (
        (game / "renpy").is_dir()
        or any(renpy_game.rglob("*.rpy"))
        or any(renpy_game.rglob("*.rpyc"))
    ):
        return "renpy"
    for archive in game.rglob("*.xp3"):
        try:
            if _read_probe(archive, len(XP3_SIGNATURE)) == XP3_SIGNATURE:
                return "kirikiri"
        except OSError:
            continue
    for executable in _executables(game):
        try:
            probe = _read_probe(executable)
        except OSError:
            continue
        if b"KIRIKIRI" in probe.upper() or b"TVP(KIRIKIRI)" in probe.upper():
            return "kirikiri"
    return None


def _decode_script(data, *, engine):
    candidates = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.extend(("utf-16",))
    if data.startswith(b"\xef\xbb\xbf"):
        candidates.extend(("utf-8-sig",))
    candidates.extend(("utf-8", "cp932"))
    for encoding in dict.fromkeys(candidates):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{SOURCE_KINDS[engine]} 脚本编码无法无损识别")


def _encode_script(text, encoding, *, engine):
    if engine == "kirikiri" and encoding in {"cp932", "utf-16"}:
        # KiriKiri recognizes a BOM and can render characters outside CP932.
        return text.encode("utf-16")
    return text.encode("utf-8-sig" if encoding == "utf-8-sig" else "utf-8")


def _quoted_spans(line):
    spans, index = [], 0
    while index < len(line):
        if line[index] not in {'"', "'"}:
            index += 1
            continue
        quote, start, index = line[index], index, index + 1
        escaped = False
        while index < len(line):
            char = line[index]
            if char == quote and not escaped:
                spans.append((start + 1, index))
                index += 1
                break
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            index += 1
    return spans


def _renpy_spans(text):
    result, offset = [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        stripped = body.lstrip()
        indentation = len(body) - len(stripped)
        if not stripped or stripped.startswith("#"):
            offset += len(line)
            continue
        spans = _quoted_spans(stripped)
        selected = None
        command = re.match(r"^([A-Za-z_]\w*)", stripped)
        if command and command.group(1) in RENPY_NON_DIALOGUE:
            spans = []
        if spans and re.match(r"^(?:[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s+)?[\"']", stripped):
            selected = spans[-1]
        elif spans and re.match(r"^[\"'].*[\"']\s*:\s*(?:#.*)?$", stripped):
            selected = spans[0]
        if selected:
            start, end = selected
            raw = stripped[start:end]
            try:
                value = ast.literal_eval(stripped[start - 1:end + 1])
            except (SyntaxError, ValueError):
                value = raw
            if value.strip():
                result.append((
                    offset + indentation + start,
                    offset + indentation + end,
                    value,
                    raw,
                ))
        offset += len(line)
    return result


def _ks_spans(text):
    result, offset = [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        stripped = body.lstrip()
        leading = len(body) - len(stripped)
        if not stripped or stripped.startswith((";", "//", "*", "@", "#")):
            offset += len(line)
            continue
        cursor = 0
        while stripped[cursor:].startswith("["):
            close = stripped.find("]", cursor + 1)
            if close < 0:
                break
            cursor = close + 1
            while cursor < len(stripped) and stripped[cursor].isspace():
                cursor += 1
        value = stripped[cursor:]
        # A tag-only line is a command, not player-visible prose.
        visible = TOKEN_RE.sub("", value).strip()
        if visible:
            result.append((offset + leading + cursor, offset + leading + len(stripped), value, value))
        else:
            for match in re.finditer(r"(?i)\b(?:text|jname)\s*=\s*([\"'])(.*?)\1", stripped):
                if match.group(2).strip():
                    result.append((
                        offset + leading + match.start(2),
                        offset + leading + match.end(2),
                        match.group(2),
                        match.group(2),
                    ))
        offset += len(line)
    return result


def _safe_internal_path(name):
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"XP3 包含不安全的内部路径：{name}")
    return path.as_posix()


@dataclass(frozen=True)
class Xp3Entry:
    name: str
    flags: int
    original_size: int
    segments: tuple


class Xp3Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = self._index()

    def _index(self):
        with self.path.open("rb") as stream:
            if stream.read(len(XP3_SIGNATURE)) != XP3_SIGNATURE:
                raise ValueError(f"不是标准 XP3：{self.path.name}")
            index_offset = struct.unpack("<Q", stream.read(8))[0]
            stream.seek(index_offset)
            index = bytearray()
            while True:
                flag_raw = stream.read(1)
                if not flag_raw:
                    raise ValueError("XP3 索引截断")
                flag = flag_raw[0]
                compressed, original = struct.unpack("<QQ", stream.read(16))
                block = stream.read(compressed)
                if len(block) != compressed:
                    raise ValueError("XP3 索引数据截断")
                block = zlib.decompress(block) if flag & 1 else block
                if len(block) != original:
                    raise ValueError("XP3 索引展开长度不一致")
                index.extend(block)
                if not flag & 0x80:
                    break
        entries, cursor = [], 0
        while cursor < len(index):
            if index[cursor:cursor + 4] != b"File":
                raise ValueError("XP3 索引含未知记录")
            size = struct.unpack_from("<Q", index, cursor + 4)[0]
            blob = memoryview(index)[cursor + 12:cursor + 12 + size]
            cursor += 12 + size
            chunks, pos = {}, 0
            while pos < len(blob):
                tag = bytes(blob[pos:pos + 4])
                length = struct.unpack_from("<Q", blob, pos + 4)[0]
                chunks.setdefault(tag, []).append(bytes(blob[pos + 12:pos + 12 + length]))
                pos += 12 + length
            info = chunks.get(b"info", [None])[0]
            if info is None or b"segm" not in chunks:
                raise ValueError("XP3 文件记录缺少 info/segm")
            flags, original_size, _stored, chars = struct.unpack_from("<IQQH", info)
            if len(info) != 22 + chars * 2 or len(chunks[b"segm"][0]) % 28:
                raise ValueError("XP3 文件记录长度无效")
            if flags & 0x80000000:
                raise ValueError("XP3 使用作品自定义加密/过滤器，当前不会冒险回包")
            name = _safe_internal_path(info[22:22 + chars * 2].decode("utf-16le"))
            segments = []
            segm = chunks[b"segm"][0]
            for at in range(0, len(segm), 28):
                segments.append(struct.unpack_from("<IQQQ", segm, at))
            entries.append(Xp3Entry(name, flags, original_size, tuple(segments)))
        return tuple(entries)

    def read(self, entry):
        parts = []
        with self.path.open("rb") as stream:
            for flags, offset, original, stored in entry.segments:
                stream.seek(offset)
                payload = stream.read(stored)
                if len(payload) != stored:
                    raise ValueError("XP3 文件数据截断")
                payload = zlib.decompress(payload) if flags & 1 else payload
                if len(payload) != original:
                    raise ValueError("XP3 文件展开长度不一致")
                parts.append(payload)
        data = b"".join(parts)
        if len(data) != entry.original_size:
            raise ValueError("XP3 文件总长度不一致")
        return data

    def rewrite(self, target, replacements):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        records = []
        with target.open("xb") as output:
            output.write(XP3_SIGNATURE + b"\0" * 8)
            for entry in self.entries:
                data = replacements.get(entry.name)
                if data is None:
                    data = self.read(entry)
                offset = output.tell()
                output.write(data)
                info = struct.pack("<IQQH", 0, len(data), len(data), len(entry.name)) + entry.name.encode("utf-16le")
                segm = struct.pack("<IQQQ", 0, offset, len(data), len(data))
                chunks = b"info" + struct.pack("<Q", len(info)) + info
                chunks += b"segm" + struct.pack("<Q", len(segm)) + segm
                adlr = struct.pack("<I", zlib.adler32(data) & 0xFFFFFFFF)
                chunks += b"adlr" + struct.pack("<Q", 4) + adlr
                records.append(b"File" + struct.pack("<Q", len(chunks)) + chunks)
            index_offset = output.tell()
            index = b"".join(records)
            output.write(b"\0" + struct.pack("<QQ", len(index), len(index)) + index)
            output.seek(len(XP3_SIGNATURE))
            output.write(struct.pack("<Q", index_offset))


def _script_documents(game, engine):
    game = Path(game)
    documents = []
    if engine == "renpy":
        scripts = sorted((game / "game").rglob("*.rpy"))
        if not scripts and any((game / "game").rglob("*.rpyc")):
            raise ValueError("该 Ren'Py 发行包只有已编译 .rpyc；当前不会反编译或伪装成可回写")
        for path in scripts:
            documents.append(("loose", path.relative_to(game).as_posix(), None, path.read_bytes()))
    elif engine == "tyranoscript":
        for path in sorted((game / "data/scenario").rglob("*.ks")):
            documents.append(("loose", path.relative_to(game).as_posix(), None, path.read_bytes()))
    else:
        loose = sorted(path for path in game.rglob("*.ks") if not any(part.startswith(".") for part in path.relative_to(game).parts))
        for path in loose:
            documents.append(("loose", path.relative_to(game).as_posix(), None, path.read_bytes()))
        for path in sorted(game.rglob("*.xp3")):
            archive = Xp3Archive(path)
            for entry in archive.entries:
                if entry.name.lower().endswith(".ks"):
                    documents.append(("xp3", path.relative_to(game).as_posix(), entry.name, archive.read(entry)))
    if not documents:
        raise ValueError(f"未找到可无损回写的 {SOURCE_KINDS[engine]} 源脚本")
    return documents


def _document_key(source_kind, source_relative, internal):
    raw = "\0".join((source_kind, source_relative, internal or ""))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def extract_game(game, output_root, job, engine):
    if engine not in SOURCE_KINDS:
        raise ValueError("未知文本引擎适配器")
    game, root = Path(game).resolve(), Path(output_root).resolve()
    executable = _executables(game)
    if not executable:
        raise ValueError(f"{SOURCE_KINDS[engine]} 游戏目录未找到可启动的 Windows EXE")
    export, corpus = root / "script-export", root / "corpus"
    export.mkdir(parents=True, exist_ok=False)
    corpus.mkdir(exist_ok=False)
    units, manifest = [], []
    container_hashes = {}
    for source_kind, source_relative, internal, data in _script_documents(game, engine):
        if source_relative not in container_hashes:
            container_hashes[source_relative] = sha256_file(game / source_relative)
        text, encoding = _decode_script(data, engine=engine)
        key = _document_key(source_kind, source_relative, internal)
        output_relative = f"documents/{key}{Path(internal or source_relative).suffix.lower()}"
        (export / output_relative).parent.mkdir(parents=True, exist_ok=True)
        # Avoid Windows newline translation changing source offsets.
        (export / output_relative).write_bytes(text.encode("utf-8"))
        spans = _renpy_spans(text) if engine == "renpy" else _ks_spans(text)
        document = {
            "key": key, "source_kind": source_kind,
            "source_relative": source_relative, "internal_path": internal,
            "output_relative": output_relative, "source_sha256": sha256_bytes(data),
            "container_sha256": container_hashes[source_relative],
            "encoding": encoding, "text_count": len(spans),
        }
        manifest.append(document)
        for start, end, value, source_text in spans:
            identifier = hashlib.sha256(f"{key}:{start}:{end}:{value}".encode()).hexdigest()[:24]
            units.append({
                "id": identifier, "text": value, "document": key,
                "source_text": source_text, "start": start, "end": end,
                "tokens": TOKEN_RE.findall(value),
            })
    if not units:
        raise ValueError(f"{SOURCE_KINDS[engine]} 脚本中未识别到可翻译正文")
    extraction = {
        "schema_version": 1, "engine": engine, "family": SOURCE_KINDS[engine],
        "game_root": str(game), "executable": executable[0].relative_to(game).as_posix(),
        "executable_sha256": sha256_file(executable[0]),
        "text_count": len(units), "documents": manifest,
    }
    save_json(corpus / "texts.json", units)
    save_json(corpus / "extraction.json", extraction)
    job.update(
        export_dir=str(export), corpus_dir=str(corpus), total_units=len(units),
        progress=0.34, message=f"已提取 {len(units)} 条文本",
        event=f"{SOURCE_KINDS[engine]} 提取完成：{len(manifest)} 个脚本、{len(units)} 条文本",
    )
    return export, corpus


def _load_translation(item):
    root = Path(item.output_root).resolve()
    export = Path(item.extraction_job.export_dir).resolve()
    corpus = Path(item.extraction_job.corpus_dir).resolve()
    snapshot = Path(item.translation_job.translated_scripts_dir).resolve()
    if any(root not in path.parents for path in (export, corpus, snapshot)):
        raise ValueError("部署必须使用本任务目录内的提取和翻译结果")
    extraction = json.loads((corpus / "extraction.json").read_text(encoding="utf-8"))
    units = json.loads((corpus / "texts.json").read_text(encoding="utf-8"))
    result = json.loads((snapshot / "translation-result.json").read_text(encoding="utf-8"))
    translations = json.loads((snapshot / "translations.json").read_text(encoding="utf-8"))
    selected = result.get("selected_ids", [])
    if (
        result.get("engine") != extraction.get("engine")
        or result.get("mode") != item.translation_job.mode
        or len(selected) != len(set(selected))
        or set(translations) != set(selected)
    ):
        raise ValueError("译文与当前引擎/选择范围不一致")
    if sha256_bytes((snapshot / "translations.json").read_bytes()) != result.get(
        "translations_sha256"
    ):
        raise ValueError("译文文件在 API 返回后发生变化")
    by_id = {unit["id"]: unit for unit in units}
    if not set(selected) <= set(by_id):
        raise ValueError("译文 ID 不属于当前提取语料")
    selected_units = [by_id[identifier] for identifier in selected]
    selected_sha = sha256_bytes(
        json.dumps(selected_units, ensure_ascii=False, sort_keys=True).encode()
    )
    if selected_sha != result.get("units_sha256"):
        raise ValueError("提取语料或翻译计划在 API 返回后发生变化")
    for identifier, translated in translations.items():
        if not isinstance(translated, str) or "\x00" in translated:
            raise ValueError("译文包含无效文本")
        if TOKEN_RE.findall(translated) != by_id[identifier]["tokens"]:
            raise ValueError("译文改变了引擎控制标签，停止回写")
    return export, corpus, snapshot, extraction, units, translations


def _render_documents(export, extraction, units, translations):
    by_document = {}
    for unit in units:
        if unit["id"] in translations:
            by_document.setdefault(unit["document"], []).append(unit)
    rendered = {}
    for document in extraction["documents"]:
        text = (export / document["output_relative"]).read_bytes().decode("utf-8")
        for unit in sorted(by_document.get(document["key"], []), key=lambda value: value["start"], reverse=True):
            if text[unit["start"]:unit["end"]] != unit.get("source_text", unit["text"]):
                raise ValueError("提取脚本与文本定位记录不一致")
            translated = translations[unit["id"]]
            if extraction["engine"] == "renpy":
                quote = text[unit["start"] - 1]
                translated = translated.replace("\\", "\\\\").replace(quote, "\\" + quote)
                translated = translated.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
            text = text[:unit["start"]] + translated + text[unit["end"]:]
        rendered[document["key"]] = _encode_script(text, document["encoding"], engine=extraction["engine"])
    return rendered


def _validate_game_tree(game):
    game = Path(game).resolve()
    files = []
    for path in game.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("原游戏包含链接/联接目录，无法安全创建独立副本")
        if path.is_file():
            files.append(path)
    return files


def _write_direct_launcher(root, executable):
    if any(char in executable for char in ('"', "%", "\r", "\n", "\x00")):
        raise ValueError("游戏启动路径含批处理无法安全表示的字符")
    value = executable.replace("/", "\\")
    content = (
        "@rem Nagi engine-native launcher v1\r\n@echo off\r\n"
        "setlocal DisableDelayedExpansion\r\nchcp 65001 >nul\r\n"
        "pushd \"%~dp0game\"\r\n"
        f'"%~dp0game\\{value}"\r\nexit /b %errorlevel%\r\n'
    )
    (root / "启动汉化版.cmd").write_bytes(content.encode("utf-8"))


def deploy_game(item, progress):
    game = Path(item.game_dir).resolve()
    export, _corpus, snapshot, extraction, units, translations = _load_translation(item)
    if item.translation_job.mode not in {"partial", "full"}:
        raise ValueError("文本引擎部署仅接受部分或全文翻译结果")
    progress(0.03, "核对原始脚本、译文范围和引擎控制标签")
    rendered = _render_documents(export, extraction, units, translations)
    executable = game / extraction["executable"]
    if not executable.is_file() or sha256_file(executable) != extraction.get(
        "executable_sha256"
    ):
        raise ValueError("游戏启动文件在提取后发生变化，请重新提取")
    verified_containers = {}
    for document in extraction["documents"]:
        container = game / document["source_relative"]
        if document["source_relative"] not in verified_containers:
            verified_containers[document["source_relative"]] = sha256_file(container)
        actual_container = verified_containers[document["source_relative"]]
        if actual_container != document.get("container_sha256"):
            raise ValueError("原始脚本容器在提取后发生变化，请重新提取")
        if document["source_kind"] == "loose":
            source = container
            if sha256_file(source) != document["source_sha256"]:
                raise ValueError("原始脚本在提取后发生变化，请重新提取")
        else:
            archive = container
            entry = next((entry for entry in Xp3Archive(archive).entries if entry.name == document["internal_path"]), None)
            if entry is None or sha256_bytes(Xp3Archive(archive).read(entry)) != document["source_sha256"]:
                raise ValueError("原始 XP3 脚本在提取后发生变化，请重新提取")
    files = _validate_game_tree(game)
    total = sum(path.stat().st_size for path in files)
    staging, output = deployment_paths(item, game)
    if shutil.disk_usage(staging.parent).free < total + 64 * 1024 * 1024:
        raise ValueError("空间不足，无法创建完整的独立汉化游戏副本")
    staging.mkdir(exist_ok=False)
    copied, manifest_files = 0, {}
    documents_by_source = {}
    for document in extraction["documents"]:
        documents_by_source.setdefault(document["source_relative"], []).append(document)
    try:
        for source in files:
            relative = source.relative_to(game).as_posix()
            # Saves are intentionally not cloned into a newly generated version.
            if any(part.casefold() in {"save", "saves", "savedata"} for part in Path(relative).parts):
                continue
            target = staging / "game" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            documents = documents_by_source.get(relative, [])
            if documents and documents[0]["source_kind"] == "xp3":
                replacements = {doc["internal_path"]: rendered[doc["key"]] for doc in documents}
                Xp3Archive(source).rewrite(target, replacements)
            elif documents and documents[0]["source_kind"] == "loose":
                target.write_bytes(rendered[documents[0]["key"]])
            else:
                shutil.copy2(source, target)
            copied += source.stat().st_size
            progress(0.08 + 0.78 * min(1, copied / max(1, total)), "正在复制独立游戏并嵌入译文")
            manifest_files["game/" + relative] = sha256_file(target)
        executable = extraction["executable"]
        if "game/" + executable not in manifest_files:
            raise ValueError("独立副本缺少已识别的游戏启动文件")
        _write_direct_launcher(staging, executable)
        manifest_files["启动汉化版.cmd"] = sha256_file(staging / "启动汉化版.cmd")
        manifest = {
            "schema_version": 1, "engine": extraction["engine"],
            "profile": f"{extraction['engine']}-text-script-v1",
            "translated_count": len(translations), "selected_count": len(translations),
            "unselected_count": len(units) - len(translations),
            "executable": "game/" + executable, "files": manifest_files,
            "original_game": str(game), "requires_font_bridge": False,
            "translation_result_sha256": sha256_file(snapshot / "translation-result.json"),
        }
        save_json(staging / "deployment.json", manifest)
        (staging / "使用说明.txt").write_text(
            f"本副本由 Nagi 从 {SOURCE_KINDS[extraction['engine']]} 脚本生成。\n"
            f"本次嵌入 {len(translations)} 条译文；原游戏和原存档未修改。\n"
            "双击“启动汉化版.cmd”启动；若作品脚本方言或字体插件异常，请保留原任务记录并停止使用本副本。\n",
            encoding="utf-8",
        )
        progress(0.96, "校验独立副本并发布固定版本入口")
        for relative, expected in manifest_files.items():
            if sha256_file(staging / relative) != expected:
                raise ValueError("独立副本发布前校验失败")
        publish_playable(staging, output)
        return output / "启动汉化版.cmd"
    except Exception:
        if staging.is_dir():
            (staging / "FAILED.txt").write_text("部署未完成，此目录不能作为汉化入口。", encoding="utf-8")
        raise
