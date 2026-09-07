"""Static Ren'Py source flow: labels, indentation, menus and loops.

No Python or Ren'Py is executed. Dynamic jumps, screens invoked as calls and
unrecognised executable statements are explicit incomplete-flow barriers.
"""

import re
from bisect import bisect_left

from .control_flow import FlowBuilder

PROFILE = "renpy-source-flow-v1"
NAME = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|\.[A-Za-z_]\w*"
LABEL = re.compile(rf"label\s+({NAME})\s*:\s*(?:#.*)?$")
CHOICE = re.compile(
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')(?:\s+if\s+.+)?\s*:\s*(?:#.*)?$"""
)


def build_program(documents, units, *, entry_ids=()):
    """Documents map extraction document keys to full decoded source strings."""
    builder = FlowBuilder()
    by_doc = {}
    for unit in units:
        by_doc.setdefault(unit["document"], []).append(unit)
    for key, text in sorted(documents.items()):
        rows, offset, namespace = [], 0, ""
        document_units = sorted(by_doc.get(key, []), key=lambda u: u["start"])
        starts = [u["start"] for u in document_units]
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            stripped = body.lstrip()
            if stripped and not stripped.startswith("#"):
                matched = document_units[
                    bisect_left(starts, offset) : bisect_left(
                        starts, offset + len(line)
                    )
                ]
                sid = matched[0]["id"] if len(matched) == 1 else None
                pc = builder.emit("unknown", sid=sid, reason="uncompiled_renpy_block")
                label = LABEL.fullmatch(stripped)
                if label:
                    name = label[1]
                    if name.startswith("."):
                        name = namespace + name if namespace else ""
                    else:
                        namespace = name
                    if name:
                        builder.label(name, pc)
                rows.append(
                    {
                        "pc": pc,
                        "text": stripped,
                        "indent": len(body) - len(stripped),
                        "namespace": namespace,
                        "invalid": "\t" in body[: len(body) - len(stripped)]
                        or len(matched) > 1,
                    }
                )
            offset += len(line)
        eof = builder.emit("return")

        def child_end(index, end):
            at = index + 1
            while at < end and rows[at]["indent"] > rows[index]["indent"]:
                at += 1
            return at

        def compile_range(start, end, continuation, loop=None):
            index = start
            while index < end:
                row = rows[index]
                pc, command = row["pc"], row["text"]
                following = child_end(index, end)
                next_pc = rows[following]["pc"] if following < end else continuation
                builder.set(pc, kind="next", next_pc=next_pc, reason=None)
                has_body = following > index + 1
                if row["invalid"]:
                    builder.set(
                        pc, kind="unknown", reason="ambiguous_indentation_or_text"
                    )
                elif LABEL.fullmatch(command):
                    if has_body:
                        builder.set(pc, next_pc=rows[index + 1]["pc"])
                        compile_range(index + 1, following, next_pc, loop)
                elif re.fullmatch(r"if\s+.+:\s*(?:#.*)?", command):
                    arms, after = [index], following
                    while (
                        after < end
                        and rows[after]["indent"] == row["indent"]
                        and re.fullmatch(
                            r"(?:elif\s+.+|else)\s*:\s*(?:#.*)?", rows[after]["text"]
                        )
                    ):
                        arms.append(after)
                        after = child_end(after, end)
                    join = rows[after]["pc"] if after < end else continuation
                    seen_else = False
                    for number, arm in enumerate(arms):
                        arm_end = child_end(arm, end)
                        arm_pc = rows[arm]["pc"]
                        is_else = rows[arm]["text"].startswith("else")
                        if seen_else or arm_end == arm + 1:
                            builder.set(
                                pc, kind="unknown", reason="malformed_conditional"
                            )
                            break
                        seen_else = is_else
                        body_pc = rows[arm + 1]["pc"]
                        if is_else:
                            builder.set(
                                arm_pc, kind="next", next_pc=body_pc, reason=None
                            )
                        else:
                            false_pc = (
                                rows[arms[number + 1]]["pc"]
                                if number + 1 < len(arms)
                                else join
                            )
                            builder.set(
                                arm_pc, kind="branch", targets=(body_pc, false_pc)
                            )
                        compile_range(arm + 1, arm_end, join, loop)
                    following = after
                elif re.fullmatch(r"menu\s*:\s*(?:#.*)?", command):
                    choices, cursor, all_guarded = [], index + 1, True
                    valid = has_body
                    while cursor < following:
                        choice = rows[cursor]
                        stop = child_end(cursor, following)
                        if not CHOICE.fullmatch(choice["text"]) or stop == cursor + 1:
                            valid = False
                            break
                        choices.append(choice["pc"])
                        all_guarded &= bool(re.search(r"\sif\s", choice["text"]))
                        builder.set(
                            choice["pc"],
                            kind="next",
                            next_pc=rows[cursor + 1]["pc"],
                            reason=None,
                        )
                        compile_range(cursor + 1, stop, next_pc, loop)
                        cursor = stop
                    if valid:
                        builder.set(
                            pc,
                            kind="branch",
                            targets=tuple(choices + ([next_pc] if all_guarded else [])),
                        )
                    else:
                        builder.set(
                            pc, kind="unknown", reason="unsupported_menu_structure"
                        )
                elif re.fullmatch(r"while\s+.+:\s*(?:#.*)?", command) and has_body:
                    builder.set(
                        pc, kind="branch", targets=(rows[index + 1]["pc"], next_pc)
                    )
                    compile_range(index + 1, following, pc, (next_pc, pc))
                elif command in {"break", "continue"} and loop:
                    builder.set(pc, kind="jump", targets=(loop[command == "continue"],))
                elif match := re.fullmatch(
                    rf"(jump|call)\s+({NAME})(?:\s+from\s+([A-Za-z_]\w*))?\s*(?:#.*)?",
                    command,
                ):
                    name = match[2]
                    if name.startswith("."):
                        name = row["namespace"] + name
                    builder.set(pc, kind="jump" if match[1] == "jump" else "call")
                    builder.transfer(pc, name)
                    if match[3] and match[1] == "call":
                        builder.label(match[3], next_pc)
                elif re.fullmatch(r"return(?:\s+[^#]+)?\s*(?:#.*)?", command):
                    builder.set(pc, kind="return")
                elif re.match(r"show\s+screen\b", command):
                    builder.set(
                        pc, kind="unknown", reason="screen_actions_require_runtime"
                    )
                elif not has_body and (
                    builder.ops[pc].segment_id is not None
                    or re.match(
                        r"(?:scene|show|hide|with|play|stop|voice|pause|window|pass|define|default)\b",
                        command,
                    )
                ):
                    # Dialogue is identified by the extraction span, not a second
                    # heuristic which could disagree about quoted expressions.
                    pass
                else:
                    builder.set(
                        pc, kind="unknown", reason="unsupported_renpy_statement"
                    )
                index = following

        compile_range(0, len(rows), eof)
    return builder.finish(
        entry="start", policy="renpy-start-label", profile=PROFILE, entry_ids=entry_ids
    )
