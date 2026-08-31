"""Narrow Agent-facing facade for pre-registered translation runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .cache import (
    TranslationRunSpec,
    execute_translation_run_next,
    inspect_translation_run,
    load_translation_checkpoint,
)
from .translator import DEFAULT_MAX_NEW_TOKENS


@dataclass(frozen=True)
class TranslationRunBinding:
    spec: TranslationRunSpec
    output_dir: str
    model_client: object | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.spec, TranslationRunSpec):
            raise TypeError("binding spec must be a TranslationRunSpec")
        if not isinstance(self.output_dir, str) or not self.output_dir.strip():
            raise ValueError("binding output_dir must be a non-empty string")
        object.__setattr__(self, "output_dir", str(Path(self.output_dir).expanduser().absolute()))


def _tool_payload(result):
    payload = result.to_dict()
    payload.pop("output_dir", None)
    return payload


class TranslationToolService:
    def __init__(self, bindings, *, default_model_client=None):
        values = tuple(bindings or ())
        if any(not isinstance(item, TranslationRunBinding) for item in values):
            raise TypeError("translation run bindings must contain TranslationRunBinding values")
        run_ids = [item.spec.run_id for item in values]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("translation run bindings must have unique run IDs")
        self.bindings = {item.spec.run_id: item for item in values}
        self.default_model_client = default_model_client

    def has_runs(self):
        return bool(self.bindings)

    def validate_run_id(self, run_id):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if run_id not in self.bindings:
            raise ValueError("translation run is not registered")
        return run_id

    def status(self, run_id):
        binding = self.bindings[self.validate_run_id(run_id)]
        result = inspect_translation_run(binding.spec, binding.output_dir)
        checkpoint = load_translation_checkpoint(binding.spec, binding.output_dir)
        selected = next(
            (
                (request, state)
                for request, state in zip(binding.spec.requests, checkpoint["batches"])
                if state["status"] != "completed"
            ),
            None,
        )
        payload = _tool_payload(result)
        payload["next"] = (
            {
                "request_id": selected[0].request_id,
                "batch_id": selected[0].batch_id,
                "status": selected[1]["status"],
                "attempts": selected[1]["attempts"],
            }
            if selected is not None
            else None
        )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def validate_next(self, run_id, request_id):
        binding = self.bindings[self.validate_run_id(run_id)]
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        checkpoint = load_translation_checkpoint(binding.spec, binding.output_dir)
        selected = next(
            (
                request
                for request, state in zip(binding.spec.requests, checkpoint["batches"])
                if state["status"] != "completed"
            ),
            None,
        )
        if selected is None:
            raise ValueError("translation run has no pending batch")
        if selected.request_id != request_id:
            raise ValueError("request_id is stale or is not the next retryable batch")
        return binding

    def execute_next(
        self,
        run_id,
        request_id,
        *,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    ):
        binding = self.validate_next(run_id, request_id)
        if (
            isinstance(max_new_tokens, bool)
            or not isinstance(max_new_tokens, int)
            or not 1 <= max_new_tokens <= 32768
        ):
            raise ValueError("max_new_tokens must be an integer in [1, 32768]")
        model_client = binding.model_client or self.default_model_client
        if model_client is None:
            raise ValueError("translation model client is not configured")
        if not callable(getattr(model_client, "complete", None)):
            raise ValueError("translation model client is invalid")
        configured_model = getattr(model_client, "model", None)
        expected_model = binding.spec.requests[0].model_id
        if configured_model is not None and str(configured_model) != expected_model:
            raise ValueError("translation model identity does not match the registered run")
        result = execute_translation_run_next(
            binding.spec,
            binding.output_dir,
            model_client,
            max_new_tokens=max_new_tokens,
        )
        return json.dumps(_tool_payload(result), ensure_ascii=False, sort_keys=True)
