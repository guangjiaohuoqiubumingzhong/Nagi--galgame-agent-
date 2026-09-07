"""QLIE control-flow adapter. Expressions are opaque, never executed.

The supported AVG dialect uses go for transfer and sub/jmp for calls, as in
the opening selector. if/else/end and case/ans/else/end describe alternatives.
Unknown commands remain explicit barriers rather than implicit fallthrough.
"""

import re
from pathlib import PurePosixPath

from ..control_flow import Instruction, Program

PROFILE = "qlie-avg-static-flow-v1"
LABEL = re.compile(r"@@(?!@)[^\s,\\]+")
TRANSFER = re.compile(r'\\(go|jmp|sub),\s*(@@[^,\s]+)(?:,\s*"([^"\r\n]+)")?\s*', re.I)
PRESENTATION = re.compile(
    r"\^(?:bgm|bgmstop|se|sestop|voice|wait|fade|bg|cg|chara|clear|messageclear)(?:[,\s]|$)",
    re.I,
)
# These change data, not the program counter. We deliberately do not evaluate
# them: correlated conditions may introduce infeasible paths in the graph.
DATA = re.compile(r"\\(?:cal|gvar|svar|del),.+", re.I)


def normalize_path(value):
    return value.replace("\\", "/").casefold()


def split_commands(text):
    """Split inline commands outside quotes, preserving quoted Windows paths."""
    starts, quote = [0], False
    for i, char in enumerate(text):
        if char == '"':
            quote = not quote
        elif char == "\\" and not quote and i and text[:i].strip():
            starts.append(i)
    if quote:
        return [""]  # Malformed quoting must not hide a transfer in a data op.
    return [text[a:b].strip() for a, b in zip(starts, [*starts[1:], len(text)])]


def build_program(scripts, *, entry_segment_ids=()):
    """Build an instruction graph from already resolved effective scripts."""
    raw, locations, labels, first_segment = [], {}, {}, {}
    for path, segments in sorted(scripts.items()):
        locations[path] = len(raw)
        local = {}
        for segment in segments:
            first_segment[segment.segment_id] = len(raw)
            text = segment.source_text.strip()
            parts = (
                split_commands(text) if segment.kind in {"control", "label"} else [text]
            )
            for part in parts:
                pc = len(raw)
                if segment.kind == "label" and LABEL.fullmatch(part):
                    key = part.casefold()
                    local[key] = None if key in local else pc
                raw.append((path, segment, part))
        raw.append((path, None, ""))  # Falling off a script returns to its caller.
        labels[path] = local

    def destination(path, label, target):
        if target:
            direct = normalize_path(target)
            relative = str(PurePosixPath(path).parent / direct)
            matches = {p for p in (direct, relative) if p in locations}
            if len(matches) != 1:
                return None
            path = matches.pop()
        local = labels[path]
        label = label.casefold()
        if label == "@@top" and label not in local:
            label = "@@main"  # Existing AVG wrapper convention, not filename order.
        return local.get(label)

    # Match structured blocks per file, including nested and inline forms.
    structures, owner, malformed = {}, {}, set()
    stack = []
    for pc, (_, segment, text) in enumerate(raw):
        if segment is None:
            malformed.update(start for start, _, _ in stack)
            stack.clear()
            continue
        if segment.kind not in {"control", "label"}:
            continue
        match = re.fullmatch(r"\\(if|case),\s*(.+)", text, re.I)
        if match:
            stack.append((pc, match[1].lower(), []))
        elif re.fullmatch(r"\\ans,\s*.+", text, re.I) or text.lower() == "\\else":
            if not stack:
                malformed.add(pc)
                continue
            start, kind, arms = stack[-1]
            if (text.lower() != "\\else" and kind != "case") or any(
                raw[a][2].lower() == "\\else" for a in arms
            ):
                malformed.update((start, pc))
            arms.append(pc)
        elif text.lower() == "\\end":
            if not stack:
                malformed.add(pc)
                continue
            start, kind, arms = stack.pop()
            structures[start] = (kind, arms, pc)
            for arm in arms:
                owner[arm] = pc

    instructions, branches = [], []
    for pc, (path, segment, text) in enumerate(raw):
        sid = segment.segment_id if segment else None
        control = segment is not None and segment.kind in {"control", "label"}
        following = pc + 1 if segment else None
        kind, targets, reason = "next", (), None
        if segment is None:
            kind = "return"
        elif pc in malformed:
            kind, reason = "unknown", "malformed_conditional"
        elif segment.kind in {
            "dialogue",
            "narration",
            "speaker_name",
            "comment",
            "metadata",
            "choice",
        }:
            pass
        elif segment.kind == "label" and LABEL.fullmatch(text):
            if labels[path][text.casefold()] is None:
                kind, reason = "unknown", "duplicate_label"
        elif pc in structures:
            block_kind, arms, end = structures[pc]
            otherwise = next((a for a in arms if raw[a][2].lower() == "\\else"), None)
            if block_kind == "if":
                targets = (pc + 1, otherwise + 1 if otherwise is not None else end + 1)
            else:
                # A case body must start with an arm; otherwise execution is unclear.
                if not arms or arms[0] != pc + 1:
                    kind, reason = "unknown", "malformed_case"
                targets = tuple(a + 1 for a in arms)
                if otherwise is None:
                    targets += (end + 1,)
            if kind != "unknown":
                kind = "branch"
                branches.append(
                    {
                        "segment_id": sid,
                        "instruction": pc,
                        "kind": block_kind,
                        "target_instructions": list(targets),
                    }
                )
        elif pc in owner:
            kind, targets = "jump", (owner[pc] + 1,)
        elif text.lower() in {"\\end", "\\then"} and control:
            # Unmatched end was marked malformed above. A standalone then is not
            # accepted; it is unnecessary in the supported block syntax.
            if text.lower() == "\\then":
                kind, reason = "unknown", "unsupported_then"
        elif text.lower() == "\\ret" and control:
            kind = "return"
        elif (match := TRANSFER.fullmatch(text)) and control:
            target = destination(path, match[2], match[3])
            if target is None:
                kind, reason = "unknown", "missing_or_ambiguous_target"
            else:
                kind = "jump" if match[1].lower() == "go" else "call"
                targets = (target,)
        elif control and (PRESENTATION.match(text) or DATA.fullmatch(text)):
            pass
        else:
            kind, reason = "unknown", "unsupported_instruction"
        instructions.append(Instruction(sid, kind, following, targets, reason))

    if entry_segment_ids:
        if len(set(entry_segment_ids)) != len(entry_segment_ids) or any(
            s not in first_segment for s in entry_segment_ids
        ):
            raise ValueError("route entry IDs must be unique effective corpus segments")
        entries = tuple(first_segment[s] for s in entry_segment_ids)
        policy = "explicit-segment-entries"
    elif "scenario/root.s" in locations:
        entries, policy = (locations["scenario/root.s"],), "qlie-scenario-root"
    elif len(locations) == 1:
        entries, policy = tuple(locations.values()), "single-script-entry"
    else:
        entries, policy = (), "unknown-entry"
    return Program(tuple(instructions), entries, tuple(branches), policy, PROFILE)
