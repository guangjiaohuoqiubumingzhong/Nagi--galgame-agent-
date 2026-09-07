"""Static KAG/TyranoScript tag control flow over complete extracted scripts."""

import re
from bisect import bisect_left
from pathlib import PurePosixPath

from .control_flow import FlowBuilder

ATTR = re.compile(r"""([\w]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+))""")
PASSIVE = frozenset(
    "p l r er cm ct wait waitclick font deffont resetfont style resetstyle position locate current image freeimage trans wt bg playbgm stopbgm fadeinbgm fadeoutbgm playse stopse quake wq chara_show chara_hide chara_mod chara_move name endlink label resetdelay delay nowait endnowait chara_new chara_delete clearfix".split()
)


def tokens(text):
    """Yield tags/labels with offsets; brackets inside quoted attributes survive."""
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        leading = len(line) - len(stripped)
        if stripped.startswith((";", "//", "#")) or not stripped.strip():
            offset += len(line)
            continue
        if stripped.startswith("*"):
            yield (
                offset + leading,
                offset + len(line),
                "label",
                stripped[1:].split("|", 1)[0].strip(),
            )
        elif stripped.startswith("@"):
            yield offset + leading, offset + len(line), "tag", stripped[1:].strip()
        else:
            index = 0
            while index < len(line):
                start = index
                if line[index] == "[":
                    index += 1
                    quote = None
                    while index < len(line):
                        char = line[index]
                        if char in {'"', "'"}:
                            quote = (
                                None
                                if quote == char
                                else (char if quote is None else quote)
                            )
                        elif char == "]" and quote is None:
                            break
                        index += 1
                    if index >= len(line):
                        yield (
                            offset + start,
                            offset + len(line),
                            "unknown",
                            "malformed_tag",
                        )
                        break
                    yield (
                        offset + start,
                        offset + index + 1,
                        "tag",
                        line[start + 1 : index],
                    )
                    index += 1
                else:
                    while index < len(line) and line[index] != "[":
                        index += 1
                    if line[start:index].strip():
                        yield offset + start, offset + index, "text", line[start:index]
        offset += len(line)


def attributes(text):
    parts = text.strip().split(None, 1)
    name = parts[0].lower() if parts else ""
    tail = parts[1] if len(parts) > 1 else ""
    values, at = {}, 0
    for match in ATTR.finditer(tail):
        if tail[at : match.start()].strip() or match[1].lower() in values:
            return name, None
        values[match[1].lower()] = next(v for v in match.groups()[1:] if v is not None)
        at = match.end()
    return name, values if not tail[at:].strip() else None


def build_program(documents, units, *, engine, entry_ids=()):
    """Documents map logical storage names to (document key, decoded script)."""
    b = FlowBuilder()
    raw, by_doc = [], {}
    for unit in units:
        by_doc.setdefault(unit["document"], []).append(unit)
    for path, (key, text) in sorted(documents.items()):
        b.label((path, ""), len(b.ops))
        lexed = list(tokens(text))
        starts = [token[0] for token in lexed]
        units_here = sorted(by_doc.get(key, ()), key=lambda u: u["start"])
        unit_starts = [u["start"] for u in units_here]
        crossing = set()
        for unit in units_here:
            # Extraction may retain inline tags inside one translation record.
            # A record spanning a transfer cannot be claimed wholly before it.
            for start, end, kind, value in lexed[
                bisect_left(starts, unit["start"]) : bisect_left(starts, unit["end"])
            ]:
                if unit["start"] < start < unit["end"] and kind == "tag":
                    name, attrs = attributes(value)
                    if (
                        name not in PASSIVE
                        or name == "endlink"
                        and end < unit["end"]
                        or attrs is None
                        or "cond" in attrs
                    ):
                        crossing.add(unit["id"])
        for start, end, kind, value in lexed:
            ids = [
                u["id"]
                for u in units_here[
                    bisect_left(unit_starts, start) : bisect_left(unit_starts, end)
                ]
            ]
            # Preserve all extracted spans in a tag, e.g. multiple name fields.
            for sid in ids[:-1]:
                raw.append((path, "text", "", b.emit(sid=sid)))
            pc = b.emit(sid=ids[-1] if ids else None)
            if set(ids) & crossing:
                kind, value = "unknown", "text_spans_control_flow"
            raw.append((path, kind, value, pc))
            if kind == "label":
                b.label((path, value), pc)
        pc = b.emit("return")
        raw.append((path, "eof", "", pc))

    # Compile if/elsif/else/endif into branch tests and skip-over edges.
    stack, structures, owner, invalid = [], {}, {}, set()
    for path, kind, text, pc in raw:
        if kind == "eof":
            invalid.update(s[0] for s in stack)
            stack.clear()
            continue
        if kind != "tag":
            continue
        name, attrs = attributes(text)
        if name == "if":
            stack.append((pc, []))
        elif name in {"elsif", "else"}:
            if not stack:
                invalid.add(pc)
            else:
                stack[-1][1].append((pc, name, attrs))
        elif name == "endif":
            if not stack:
                invalid.add(pc)
            else:
                start, arms = stack.pop()
                structures[start] = (arms, pc)
                for arm, _, _ in arms:
                    owner[arm] = pc

    def target(path, attrs):
        storage = attrs.get("storage", "").replace("\\", "/")
        label = attrs.get("target", "").lstrip("*")
        if storage.startswith(("&", "%")) or label.startswith(("&", "%")):
            return None
        if storage:
            options = {
                name
                for name in (storage, str(PurePosixPath(path).parent / storage))
                if name in documents
            }
            if len(options) != 1:
                return None
            path = options.pop()
        return (path, label)

    links, open_link = [], None
    for path, kind, text, pc in raw:
        if kind in {"eof", "label"}:
            if links or open_link is not None:
                b.set(pc, kind="unknown", reason="interaction_crosses_script_boundary")
            links = []
            open_link = None
            continue
        if kind == "text":
            continue
        name, attrs = attributes(text) if kind == "tag" else ("", None)
        if attrs is None or pc in invalid:
            b.set(pc, kind="unknown", reason="malformed_tag_or_conditional")
        elif "cond" in attrs and name not in {"jump", "call"}:
            b.set(pc, kind="unknown", reason="conditional_tag_requires_state")
        elif links and name in {"jump", "call", "return"}:
            b.set(pc, kind="unknown", reason="transfer_with_live_links")
        elif pc in structures:
            arms, end = structures[pc]
            if (
                not attrs.get("exp")
                or any(a is None for _, _, a in arms)
                or sum(n == "else" for _, n, _ in arms) > 1
                or any(n == "else" for _, n, _ in arms[:-1])
            ):
                b.set(pc, kind="unknown", reason="malformed_conditional")
                continue
            # Arm markers skip to join on fallthrough. False edges go to a
            # separate test node so an elsif body never falls into the next arm.
            false_target = end + 1
            for arm, arm_name, arm_attrs in reversed(arms):
                b.set(arm, kind="jump", targets=(end + 1,))
                if arm_name == "else":
                    false_target = arm + 1
                elif arm_attrs.get("exp"):
                    test = b.emit("branch", targets=(arm + 1, false_target))
                    false_target = test
                else:
                    b.set(pc, kind="unknown", reason="missing_condition")
            if b.ops[pc].kind != "unknown":
                b.set(pc, kind="branch", targets=(pc + 1, false_target))
        elif pc in owner:
            pass  # Patched by its owning if; never overwrite its join edge.
        elif name == "endif":
            pass
        elif name in {"jump", "call"}:
            label = target(path, attrs)
            if label is None or "exp" in attrs:
                b.set(pc, kind="unknown", reason="dynamic_transfer")
            elif "cond" in attrs:
                transfer = b.emit("jump" if name == "jump" else "call")
                b.set(transfer, next_pc=pc + 1)
                b.transfer(transfer, label)
                b.set(pc, kind="branch", targets=(transfer, pc + 1))
            else:
                b.set(pc, kind="jump" if name == "jump" else "call")
                b.transfer(pc, label)
        elif name == "return" and not attrs:
            b.set(pc, kind="return")
        elif name in {"link", "glink", "button"}:
            label = target(path, attrs)
            if (
                label is None
                or any(
                    k in attrs for k in ("exp", "preexp", "role", "sleepgame", "call")
                )
                or not (attrs.get("target") or attrs.get("storage"))
            ):
                b.set(pc, kind="unknown", reason="dynamic_or_scripted_choice")
            else:
                links.append(label)
                if name == "link":
                    if open_link is not None:
                        b.set(pc, kind="unknown", reason="nested_link")
                    open_link = label
                else:
                    arm = b.emit("jump")
                    b.transfer(arm, label)
                    b.set(pc, kind="branch", targets=(pc + 1, arm))
        elif name == "endlink":
            if open_link is None:
                b.set(pc, kind="unknown", reason="unmatched_endlink")
            else:
                # Links may be clicked before the subsequent [s]. Include that
                # edge so later menu text is never falsely guaranteed history.
                arm = b.emit("jump")
                b.transfer(arm, open_link)
                b.set(pc, kind="branch", targets=(pc + 1, arm))
                open_link = None
        elif name == "s":
            if not links:
                b.set(pc, kind="unknown", reason="unresolved_interaction")
            else:
                alternatives = []
                for label in links:
                    arm = b.emit("jump")
                    b.transfer(arm, label)
                    alternatives.append(arm)
                b.set(pc, kind="branch", targets=tuple(alternatives))
                links = []
        elif name in PASSIVE:
            if name in {"cm", "ct"}:
                links = []
        else:
            # eval/iscript/macros can perform jumps or modify the call stack.
            b.set(pc, kind="unknown", reason="unsupported_kag_tag")
    roots = [path for path in documents if PurePosixPath(path).name == "first.ks"]
    entry = (
        (roots[0], "")
        if len(roots) == 1
        else ((next(iter(documents)), "") if len(documents) == 1 else None)
    )
    return b.finish(
        entry=entry,
        policy=f"{engine}-first-script",
        profile=f"{engine}-tag-flow-v1",
        entry_ids=entry_ids,
    )
