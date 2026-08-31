# Nagi Review Pack

## Project pitch

Nagi is a lightweight local coding agent harness for repository-grounded engineering tasks. It wraps a model with workspace context, explicit tools, state tracking, memory, run artifacts, and benchmark evidence.

## Architecture map

- `nagi.cli` wires configuration, provider clients, workspace context, and the runtime.
- `nagi.runtime.Nagi` coordinates the agent control surface.
- `nagi.context_manager` builds bounded model context from prefix, memory, history, and the current request.
- `nagi.tools` defines the explicit tool allowlist used by the runtime.
- `nagi.run_store` writes per-run artifacts for review and replay.

## Benchmark evidence

Benchmark runs should preserve reproducibility metadata, task rows, summary counts, and failure categories so reviewers can distinguish runtime regressions from task or provider failures.

## Sample run artifact list

- `.nagi/runs/<run_id>/task_state.json`
- `.nagi/runs/<run_id>/trace.jsonl`
- `.nagi/runs/<run_id>/report.json`
