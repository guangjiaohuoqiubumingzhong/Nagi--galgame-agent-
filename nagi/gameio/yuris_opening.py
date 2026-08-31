"""Resolve ERIS's main-story label, then follow static YU-RIS 479 story flow."""

import struct
from collections import defaultdict

from .opening import LIMIT, selection_receipt, unsupported
from .yuris import literal, parse_script


def label_table(data, scripts):
    # YSLB: header, 256 hash-bucket indices, then length/name/hash/pc/script rows.
    if len(data) < 1036 or data[:4] != b"YSLB":
        raise unsupported("缺少有效的 YU-RIS 标签表")
    version, count = struct.unpack_from("<II", data, 4)
    buckets = struct.unpack_from("<256I", data, 12)
    if version != 479 or count > 100000 or list(buckets) != sorted(buckets) or buckets[-1] > count:
        raise unsupported("YU-RIS 标签表版本或范围无效")
    labels, offset = {}, 1036
    try:
        for _ in range(count):
            length = data[offset]
            offset += 1
            name = data[offset:offset + length].decode("cp932")
            offset += length
            _, pc, script = struct.unpack_from("<III", data, offset)
            offset += 12
            matches = [path for path in scripts if path.rsplit("/", 1)[-1].casefold() == f"yst{script:05d}.ybn"]
            if not name or name in labels or len(matches) != 1 or pc >= len(scripts[matches[0]]):
                raise unsupported("YU-RIS 标签存在歧义或目标越界")
            labels[name] = (matches[0], pc)
    except (IndexError, struct.error, UnicodeDecodeError) as exc:
        raise unsupported("YU-RIS 标签表不完整") from exc
    if offset != len(data):
        raise unsupported("YU-RIS 标签表长度不一致")
    return labels


def _strings(instruction):
    return {
        instruction["parameters"][field["id"]]: literal(field["blob"])
        for field in instruction["fields"]
        if field["type"] == 3 and field["id"] < len(instruction["parameters"])
    }


def select_opening(entries, table, key, units):
    scripts = {e["name"]: parse_script(e["content"], key, table)[0]
               for e in entries if e["content"][:4] == b"YSTB"}
    label_files = [e for e in entries if e["content"][:4] == b"YSLB"]
    if len(label_files) > 1:
        raise unsupported("存在多份 YU-RIS 标签表")
    labels = label_table(label_files[0]["content"], scripts) if label_files else {}
    # ERIS separates its title/resume/bonus dispatcher from the new-game story.
    if "SCENARIO_MAIN" in labels:
        entry = "SCENARIO_MAIN"
        cursor = labels[entry]
    elif not labels and len(scripts) == 1:
        entry = next(iter(scripts))
        cursor = (entry, 0)
    else:
        raise unsupported("未识别到新游戏的 SCENARIO_MAIN 入口")
    by_instruction = defaultdict(list)
    for unit in units:
        if unit["command"] == "WORD":
            by_instruction[(unit["script"], unit["instruction"])].append(unit)
    selected, seen, stack = [], set(), []
    while len(selected) < LIMIT:
        if cursor in seen:
            raise unsupported("开场路径出现循环")
        seen.add(cursor)
        path, pc = cursor
        if pc >= len(scripts[path]):
            if not stack:
                break
            cursor = stack.pop()
            continue
        instruction = scripts[path][pc]
        command = instruction["command"]
        cursor = (path, pc + 1)
        if command == "WORD":
            selected.extend(by_instruction[(path, pc)])
        elif command in {"IF", "LOOP", "SWITCH", "ELSE", "IFBLEND"}:
            raise unsupported("开场含尚未支持的条件分支")
        elif command in {"END", "RETURN"}:
            if not stack:
                break
            cursor = stack.pop()
        elif command in {"GO", "GOSUB"}:
            target = _strings(instruction).get("#")
            if command == "GOSUB" and target and target.upper().startswith(("ES.", "MAC.")):
                if target.upper().startswith("ES.SEL."):
                    raise unsupported("开场在前 50 条内需要玩家选择")
                # Engine display/registration macros are not story records.
                continue
            if not target or target not in labels:
                raise unsupported("开场跳转目标是动态值或缺失标签")
            if command == "GOSUB":
                stack.append(cursor)
            cursor = labels[target]
    if not selected:
        raise unsupported("开场入口未包含对白或旁白")
    selected = selected[:LIMIT]
    return selected, selection_receipt("yuris-479", entry, (u["id"] for u in selected))


def selected_for_result(entries, table, key, units, result):
    """Old paid runs retain their extraction-order scope; new receipts are verified."""
    receipt = result.get("opening_selection")
    if receipt is None:
        return units[:LIMIT]
    selected, expected = select_opening(entries, table, key, units)
    if receipt != expected:
        raise ValueError("开场剧情选取记录不匹配，停止部署；请重新翻译")
    return selected
