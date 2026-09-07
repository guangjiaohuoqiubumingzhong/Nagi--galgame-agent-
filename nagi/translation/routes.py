"""Entry-relative may/must history on a context-sensitive control-flow graph.

May is a union and must is an intersection over predecessor histories. The
fixed point is equivalent to labelled-node dominance, including loops and
separate call stacks. Conditions are nondeterministic: may means possible in
the static graph, not proven feasible under a game's variable constraints.
"""

from collections import defaultdict, deque
from pathlib import Path

from ..gameio.qlie.flow import build_program
from ..gameio.qlie.opening import _effective_scripts

ROUTE_VERSION = "route-history-v1"
MAX_STATES = 20000
MAX_CALL_DEPTH = 16


class RouteAnalysis:
    def __init__(
        self,
        program,
        text_segment_ids,
        *,
        max_states=MAX_STATES,
        max_call_depth=MAX_CALL_DEPTH,
    ):
        self.program = program
        self.ids = tuple(sorted(set(text_segment_ids)))
        self.valid_ids = set(self.ids)
        self.diagnostics = []
        self.by_segment = defaultdict(list)
        self.states, self.successors, self.predecessors = [], [], []
        self.roots = set()
        indexes, pending = {}, deque()

        def intern(state):
            if state not in indexes:
                if len(indexes) >= max_states:
                    raise OverflowError("state_limit")
                indexes[state] = len(self.states)
                self.states.append(state)
                self.successors.append(set())
                self.predecessors.append(set())
                pending.append(indexes[state])
            return indexes[state]

        try:
            for pc in program.entries:
                self.roots.add(intern((pc, ())))
            while pending:
                index = pending.popleft()
                pc, stack = self.states[index]
                op = program.instructions[pc]
                self.by_segment[op.segment_id].append(index)
                following = []
                if op.kind == "unknown":
                    self.diagnostics.append(
                        {"segment_id": op.segment_id, "reason": op.reason}
                    )
                elif op.kind == "return":
                    if stack:
                        following = [(stack[-1], stack[:-1])]
                elif op.kind == "call":
                    if len(stack) >= max_call_depth:
                        self.diagnostics.append(
                            {"segment_id": op.segment_id, "reason": "call_depth_limit"}
                        )
                    else:
                        following = [(op.targets[0], (*stack, op.next_pc))]
                elif op.kind in {"jump", "branch"}:
                    following = [(target, stack) for target in op.targets]
                elif op.next_pc is not None:
                    following = [(op.next_pc, stack)]
                for state in following:
                    target = intern(state)
                    self.successors[index].add(target)
                    self.predecessors[target].add(index)
        except OverflowError:
            self.diagnostics.append({"segment_id": None, "reason": "state_limit"})
        if not program.entries:
            self.diagnostics.append({"segment_id": None, "reason": "unknown_entry"})
        # Missing/dynamic edges could reach anywhere. No universal claim is safe
        # when a reachable instruction or entry is unresolved.
        self.complete = not self.diagnostics
        # Allocate bits only to reachable text. An enormous disconnected corpus
        # must not allocate quadratic-size bit masks before the state limit.
        reachable_ids = sorted(
            {program.instructions[pc].segment_id for pc, _ in self.states}
            & self.valid_ids
        )
        self.bits = {sid: 1 << i for i, sid in enumerate(reachable_ids)}
        self._solve()

    def _solve(self):
        count = len(self.states)
        universal = (1 << len(self.bits)) - 1
        self.may = [0] * count
        self.must = [universal if self.complete else 0 for _ in range(count)]
        own = [
            self.bits.get(self.program.instructions[pc].segment_id, 0)
            for pc, _ in self.states
        ]
        queue, queued = deque(range(count)), set(range(count))
        while queue:
            index = queue.popleft()
            queued.remove(index)
            possible = 0
            necessary = 0 if index in self.roots or not self.complete else universal
            for predecessor in self.predecessors[index]:
                possible |= self.may[predecessor] | own[predecessor]
                if self.complete:
                    necessary &= self.must[predecessor] | own[predecessor]
            if possible != self.may[index] or necessary != self.must[index]:
                self.may[index], self.must[index] = possible, necessary
                for target in self.successors[index]:
                    if target not in queued:
                        queue.append(target)
                        queued.add(target)

    def history_masks(self, target_id):
        states = self.by_segment.get(target_id, ())
        if not states:
            return 0, 0
        possible, necessary = 0, self.must[states[0]]
        for state in states:
            possible |= self.may[state]
            necessary &= self.must[state]
        # A repeated visit to the same line is never evidence for itself.
        mask = ~self.bits.get(target_id, 0)
        return possible & mask, necessary & mask

    def classify(self, source_id, target_id):
        if source_id not in self.valid_ids or target_id not in self.valid_ids:
            raise ValueError("history IDs must identify corpus dialogue or narration")
        if source_id == target_id:
            return "self"
        possible, necessary = self.history_masks(target_id)
        bit = self.bits.get(source_id, 0)
        if necessary & bit:
            return "must"
        if possible & bit:
            return "may"
        return "unreachable" if self.complete else "unknown"

    def history(self, target_id):
        if target_id not in self.valid_ids:
            raise ValueError("target ID must identify corpus dialogue or narration")
        possible, necessary = self.history_masks(target_id)
        return {
            "target_segment_id": target_id,
            "target_status": "reachable"
            if self.by_segment.get(target_id)
            else ("unreachable" if self.complete else "unknown"),
            "must": [sid for sid, bit in self.bits.items() if bit & necessary],
            "may_only": [
                sid
                for sid, bit in self.bits.items()
                if bit & possible and not bit & necessary
            ],
            "complete": self.complete,
        }

    def summary(self):
        unique = {(d["segment_id"], d["reason"]) for d in self.diagnostics}
        diagnostics = [
            {"segment_id": sid, "reason": reason}
            for sid, reason in sorted(unique, key=str)
        ]
        return {
            "version": ROUTE_VERSION,
            "profile": self.program.profile,
            "status": "complete" if self.complete else "incomplete",
            "semantics": "all-static-paths-with-opaque-conditions",
            "entry_policy": self.program.entry_policy,
            "entry_segment_ids": [
                self.program.instructions[pc].segment_id for pc in self.program.entries
            ],
            "state_count": len(self.states),
            "branch_count": len(self.program.branches),
            "diagnostic_count": len(diagnostics),
            "diagnostics": diagnostics[:100],
        }


def analyze_corpus_routes(plan, *, entry_segment_ids=()):
    """Return QLIE analysis; unsupported engines keep their local scope policy."""
    units = [u for b in plan.batches for u in b.units]
    if not units or {u.source.engine for u in units} != {"qlie"}:
        if entry_segment_ids:
            raise ValueError("explicit route entries require a QLIE corpus")
        return None
    scripts = _effective_scripts(
        Path(plan.segments_path).parent, expected_segments_sha256=plan.segments_sha256
    )
    program = build_program(scripts, entry_segment_ids=entry_segment_ids)
    return RouteAnalysis(
        program, [u.segment_id for u in units if u.kind in {"dialogue", "narration"}]
    )
