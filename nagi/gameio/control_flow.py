"""Engine-neutral control-flow instructions and a small assembly helper."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Instruction:
    segment_id: str | None
    kind: str
    next_pc: int | None
    targets: tuple[int, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class Program:
    instructions: tuple[Instruction, ...]
    entries: tuple[int, ...]
    branches: tuple[dict, ...]
    entry_policy: str
    profile: str = "generic-static-flow-v1"


class FlowBuilder:
    def __init__(self):
        self.ops = []
        self.labels = {}
        self.transfers = []

    def emit(self, kind="next", sid=None, *, targets=(), reason=None):
        pc = len(self.ops)
        self.ops.append(Instruction(sid, kind, pc + 1, tuple(targets), reason))
        return pc

    def set(self, pc, **changes):
        self.ops[pc] = replace(self.ops[pc], **changes)

    def label(self, name, pc):
        if name in self.labels:
            self.labels[name] = None
        else:
            self.labels[name] = pc

    def transfer(self, pc, name):
        self.transfers.append((pc, name))

    def finish(self, *, entry, policy, profile, entry_ids=()):
        for pc, name in self.transfers:
            target = self.labels.get(name)
            if target is None:
                self.set(pc, kind="unknown", reason="missing_or_ambiguous_target")
            else:
                self.set(pc, targets=(target,))
        if entry_ids:
            if len(set(entry_ids)) != len(entry_ids):
                raise ValueError("route entry IDs must be unique")
            first = {}
            for pc, op in enumerate(self.ops):
                if op.segment_id:
                    first.setdefault(op.segment_id, pc)
            if any(sid not in first for sid in entry_ids):
                raise ValueError("route entry ID is not in the effective script")
            entries = tuple(first[sid] for sid in entry_ids)
            policy = "explicit-segment-entries"
        else:
            start = self.labels.get(entry)
            entries = (start,) if start is not None else ()
        branches = tuple(
            {
                "segment_id": op.segment_id,
                "instruction": pc,
                "kind": "branch",
                "target_instructions": list(op.targets),
            }
            for pc, op in enumerate(self.ops)
            if op.kind == "branch"
        )
        return Program(tuple(self.ops), entries, branches, policy, profile)
