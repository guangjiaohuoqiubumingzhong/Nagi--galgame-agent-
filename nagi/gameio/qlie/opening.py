"""Opening dialogue selection for QLIE's Scenario/Root.s dispatcher convention."""

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from ..opening import LIMIT, selection_receipt, unsupported
from ..segments import TextSegment


def _path(value):
    return value.replace("\\", "/").casefold()


def _effective_scripts(corpus, *, expected_segments_sha256=None):
    corpus = Path(corpus)
    raw = (corpus / "segments.jsonl").read_bytes()
    if expected_segments_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_segments_sha256:
        raise unsupported("QLIE 语料与已验证的翻译计划不匹配")
    report = json.loads((corpus / "parse-report.json").read_text(encoding="utf-8"))
    if report["artifacts"]["segments"]["sha256"] != hashlib.sha256(raw).hexdigest():
        raise unsupported("QLIE 语料已改变")
    by_path = defaultdict(lambda: defaultdict(list))
    for line in raw.splitlines():
        segment = TextSegment.from_dict(json.loads(line))
        by_path[_path(segment.source.internal_path)][segment.source.output_path].append(segment)
    ranks = {}
    manifest_path = corpus / "source-manifest.json"
    if manifest_path.is_file():
        manifest_raw = manifest_path.read_bytes()
        if report["artifacts"].get("source_manifest", {}).get("sha256") != hashlib.sha256(manifest_raw).hexdigest():
            raise unsupported("QLIE 来源清单已改变")
        manifest = json.loads(manifest_raw)
        order = manifest.get("archive_order", [])
        if manifest.get("archive_order_explicit"):
            ranks = {name: index for index, name in enumerate(order)}
    scripts = {}
    for path, variants in by_path.items():
        groups = list(variants.values())
        if len(groups) > 1:
            if any(group[0].source.archive_name not in ranks for group in groups):
                raise unsupported("QLIE 重名脚本没有明确的资源覆盖顺序")
            groups.sort(key=lambda g: ranks[g[0].source.archive_name])
            if ranks[groups[-1][0].source.archive_name] == ranks[groups[-2][0].source.archive_name]:
                raise unsupported("QLIE 同层存在重复脚本")
        scripts[path] = sorted(groups[-1], key=lambda s: (s.source.line_start, s.source.byte_start))
    return scripts


def select_opening(corpus):
    scripts = _effective_scripts(corpus)
    entry = "scenario/root.s"
    if entry not in scripts:
        # Single-file standalone stories need no dispatcher or filename guessing.
        if len(scripts) == 1:
            entry = next(iter(scripts))
        else:
            raise unsupported("未识别到 QLIE 的 Scenario/Root.s 开场入口")
    labels = {}
    for path, segments in scripts.items():
        local = {}
        for index, segment in enumerate(segments):
            value = segment.source_text.strip()
            if segment.kind == "label" and value.startswith("@@") and not value.startswith("@@@"):
                name = value.casefold()
                if name in local:
                    raise unsupported("QLIE 脚本存在重复标签")
                local[name] = index
        labels[path] = local
    cursor, seen, stack, selected, display_ids = (entry, 0), set(), [], [], []
    while len(selected) < LIMIT:
        if cursor in seen:
            raise unsupported("QLIE 开场路径出现循环")
        seen.add(cursor)
        path, index = cursor
        if index >= len(scripts[path]):
            if not stack:
                break
            cursor = stack.pop()
            continue
        segment = scripts[path][index]
        cursor = (path, index + 1)
        text = segment.source_text.strip()
        if segment.kind == "speaker_name" and segment.translatable:
            display_ids.append(segment.segment_id)
        elif segment.kind in {"dialogue", "narration"} and segment.translatable:
            if not re.fullmatch(r"[【〖][^\r\n【】〖〗]+[】〗]", text):
                selected.append(segment.segment_id)
        elif segment.kind == "choice" or re.match(r"\\(?:if|while|switch|select)\b", text, re.IGNORECASE):
            raise unsupported("QLIE 开场含尚未支持的条件分支或玩家选项")
        elif re.match(r"\\ret\b", text, re.IGNORECASE):
            if not stack:
                break
            cursor = stack.pop()
        elif re.match(r"\\(?:go|jmp|sub),", text, re.IGNORECASE):
            match = re.fullmatch(r'\\(go|jmp|sub),\s*(@@[^,\s]+)(?:,\s*"([^"\r\n]+)")?\s*', text, re.IGNORECASE)
            if not match:
                raise unsupported("QLIE 开场存在动态跳转")
            command, label, destination = match.groups()
            target = _path(destination) if destination else path
            # System setup calls are not narrative; never count their UI strings.
            if destination and command.lower() == "sub" and not target.startswith("scenario/"):
                continue
            if target not in scripts and destination:
                target = str(PurePosixPath(path).parent / target)
            if target not in scripts:
                raise unsupported("QLIE 开场目标脚本缺失")
            local = labels[target]
            label = label.casefold()
            if label in local:
                next_index = local[label]
            elif label == "@@top" and "@@main" in local:
                # AVG/header.s exposes @@Top and dispatches to the story's @@MAIN.
                next_index = local["@@main"]
            else:
                raise unsupported("QLIE 开场目标标签缺失")
            if command.lower() in {"sub", "jmp"}:
                stack.append(cursor)
            cursor = (target, next_index)
    if not selected:
        raise unsupported("QLIE 开场未找到对白或旁白")
    return {**selection_receipt("qlie", entry, selected), "display_ids": display_ids}
