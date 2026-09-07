"""YU-RIS 479 control flow from decoded bytecode and its actual label table."""

from collections import defaultdict

from .control_flow import FlowBuilder
from .yuris import parse_script
from .yuris_opening import _strings, label_table


def build_program(entries, table, key, units, *, entry_ids=()):
    scripts = {
        e["name"]: parse_script(e["content"], key, table)[0]
        for e in entries
        if e["content"][:4] == b"YSTB"
    }
    label_files = [e for e in entries if e["content"][:4] == b"YSLB"]
    if len(label_files) > 1:
        raise ValueError("Ambiguous YU-RIS label tables")
    labels = label_table(label_files[0]["content"], scripts) if label_files else {}
    b, locations, raw = FlowBuilder(), {}, []
    texts = defaultdict(list)
    for unit in units:
        texts[(unit["script"], unit["instruction"])].append(unit["id"])
    for path, instructions in sorted(scripts.items()):
        for index, instruction in enumerate(instructions):
            locations[path, index] = len(b.ops)
            for sid in texts[path, index]:
                b.emit(sid=sid)
            pc = b.emit()
            raw.append((path, instruction, pc))
        b.emit("return")
    for name, location in labels.items():
        b.label(name, locations[location])
    if not labels and len(scripts) == 1 and locations:
        b.label("SCENARIO_MAIN", next(iter(locations.values())))
    # Structured blocks are matched within a script, never across files.
    stack, blocks, owners, invalid = [], {}, {}, set()
    last_path = None
    for path, instruction, pc in raw:
        if last_path != path:
            invalid.update(start for _, start, _ in stack)
            stack.clear()
            last_path = path
        command = instruction["command"].upper()
        if command in {"IF", "LOOP"}:
            stack.append((command, pc, []))
        elif command == "ELSE":
            if (
                not stack
                or stack[-1][0] != "IF"
                or any(not conditional for _, conditional in stack[-1][2])
            ):
                invalid.add(pc)
            else:
                stack[-1][2].append((pc, bool(instruction["fields"])))
        elif command in {"IFEND", "LOOPEND"}:
            expected = "IF" if command == "IFEND" else "LOOP"
            if not stack or stack[-1][0] != expected:
                invalid.add(pc)
            else:
                kind, start, arms = stack.pop()
                blocks[start] = (kind, pc, arms)
                owners[pc] = start
                for arm, _ in arms:
                    owners[arm] = start
        elif command in {"LOOPBREAK", "LOOPCONTINUE", "IFBREAK", "IFCONTINUE"}:
            expected = "LOOP" if command.startswith("LOOP") else "IF"
            loop = next(
                (start for kind, start, _ in reversed(stack) if kind == expected), None
            )
            # LV may select an outer block; never assume the nearest one when
            # parameter bytecode is present and has not been evaluated.
            if loop is None or instruction["fields"]:
                invalid.add(pc)
            else:
                owners[pc] = loop
    invalid.update(start for _, start, _ in stack)
    for path, instruction, pc in raw:
        command = instruction["command"].upper()
        if pc in invalid:
            b.set(pc, kind="unknown", reason="malformed_yuris_block")
        elif pc in blocks:
            kind, end, arms = blocks[pc]
            false_target = end + 1
            for arm, conditional in reversed(arms):
                if conditional:
                    false_target = b.emit("branch", targets=(arm + 1, false_target))
                else:
                    false_target = arm + 1
            b.set(pc, kind="branch", targets=(pc + 1, false_target))
        elif command in {
            "ELSE",
            "IFEND",
            "LOOPEND",
            "LOOPBREAK",
            "LOOPCONTINUE",
            "IFBREAK",
            "IFCONTINUE",
        }:
            start = owners.get(pc)
            if start not in blocks:
                b.set(pc, kind="unknown", reason="unmatched_yuris_block")
            elif command != "IFEND":
                end = blocks[start][1]
                target = (
                    start
                    if command in {"LOOPEND", "LOOPCONTINUE", "IFCONTINUE"}
                    else end + 1
                )
                b.set(pc, kind="jump", targets=(target,))
        elif command in {"GO", "GOSUB"}:
            target = _strings(instruction).get("#")
            if target is None:
                b.set(pc, kind="unknown", reason="dynamic_yuris_transfer")
            else:
                b.set(pc, kind="jump" if command == "GO" else "call")
                b.transfer(pc, target)
        elif command in {"END", "RETURN"}:
            b.set(pc, kind="return")
        elif command in {"WORD", "LET", "WAIT", "NOP"}:
            pass
        else:
            b.set(pc, kind="unknown", reason="unsupported_yuris_command")
    return b.finish(
        entry="SCENARIO_MAIN",
        policy="yuris-main-story-label",
        profile="yuris-479-bytecode-flow-v1",
        entry_ids=entry_ids,
    )
