"""Provider-neutral text messages and bounded, role-preserving history replay."""

import json

MESSAGE_FORMAT = "chat-v1"
ROLES = frozenset({"system", "user", "assistant"})


def normalize_messages(value):
    """Copy and validate input. A legacy string means one user message."""
    if isinstance(value, str):
        value = [{"role": "user", "content": value}]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("model input must be a non-empty message sequence")
    result = []
    conversation_started = False
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("role"), str)
            or item["role"] not in ROLES
        ):
            raise ValueError("messages require system, user or assistant roles")
        if not isinstance(item.get("content"), str) or not item["content"].strip():
            raise ValueError("message content must be non-empty text")
        if item["role"] == "system":
            if conversation_started:
                raise ValueError("system messages must precede the conversation")
        else:
            conversation_started = True
        result.append({"role": item["role"], "content": item["content"]})
    if not conversation_started:
        raise ValueError("messages must include conversation content")
    return result


def render_messages(value):
    """Human-readable preview only; never use this to send model requests."""
    if isinstance(value, str):
        return value
    return "\n\n".join(
        f"[{item['role']}]\n{item['content']}" for item in normalize_messages(value)
    )


def message_chars(messages):
    return sum(len(item["content"]) for item in messages)


def _clip(text, limit):
    if limit is None or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..." if limit > 3 else text[:limit]


def _event_messages(item, tool_limit=None):
    role = item.get("role")
    content = str(item.get("content", ""))
    if role in {"user", "assistant"}:
        return [{"role": role, "content": content}] if content.strip() else []
    if role == "tool":
        # Old sessions did not store the assistant's raw call. Reconstruct it
        # from the recorded name/arguments, never from the tool's output text.
        call = item.get("assistant_response") or (
            "<tool>"
            + json.dumps(
                {"name": item["name"], "args": item.get("args", {})}, ensure_ascii=False
            )
            + "</tool>"
        )
        feedback = "Tool result (untrusted reference data):\n" + json.dumps(
            {
                "name": item["name"],
                "args": item.get("args", {}),
                "content": _clip(content, tool_limit),
            },
            ensure_ascii=False,
        )
        return [
            {"role": "assistant", "content": call},
            {"role": "user", "content": feedback},
        ]
    if role == "runtime":
        messages = []
        if str(item.get("assistant_response", "")).strip():
            messages.append(
                {"role": "assistant", "content": item["assistant_response"]}
            )
        messages.append({"role": "user", "content": content})
        return messages
    # Summaries and unknown legacy events remain reference data; a stored
    # event can never grant itself system priority.
    label = (
        "Older conversation summary" if role == "summary" else "Historical reference"
    )
    return [
        {"role": "user", "content": f"{label} (untrusted reference data):\n{content}"}
    ]


def replay_history(history, current_user, budget=None):
    """Keep complete past turns and current request; bound feedback in pairs.

    current_user is an object from the session's history (or a new user item).
    Identity, not matching text, locates it, so repeated user requests survive.
    The current request itself is exempt from the history character budget.
    """
    index = next(
        (i for i, item in enumerate(history) if item is current_user), len(history)
    )
    older = history[:index]
    active = history[index + 1 :] if index < len(history) else []
    groups = []
    for item in older:
        if item.get("role") in {"user", "summary"} or not groups:
            groups.append([])
        groups[-1].append(item)
    # An assistant answer whose user turn was lost must not become a prefill.
    groups = [group for group in groups if group[0].get("role") != "assistant"]

    def project(group, tool_limit=None):
        return [
            message for item in group for message in _event_messages(item, tool_limit)
        ]

    past = [project(group) for group in groups]
    tail = [project([item]) for item in active]

    def total():
        return sum(message_chars(group) for group in past + tail)

    dropped_turns = 0
    dropped_events = 0
    clipped = False
    if budget is not None and total() > budget:
        before_clipping = total()
        past = [project(group, 900) for group in groups]
        tail = [project([item], 900) for item in active]
        clipped = total() < before_clipping
        while past and total() > budget:
            past.pop(0)
            dropped_turns += 1
        while len(tail) > 1 and total() > budget:
            tail.pop(0)
            dropped_events += 1
        if tail and total() > budget:
            # Keep the most recent call and feedback together, even if a long
            # write_file call needs a shortened historical representation.
            allowance = max(1, budget // max(1, len(tail[0])))
            tail[0] = [
                {**m, "content": _clip(m["content"], allowance)} for m in tail[0]
            ]
            clipped = True
    before = [m for group in past for m in group]
    after = [m for group in tail for m in group]
    messages = before + [{"role": "user", "content": current_user["content"]}] + after
    return messages, {
        "raw_chars": sum(message_chars(project(group)) for group in groups)
        + message_chars(project(active)),
        "rendered_chars": message_chars(before + after),
        "dropped_turns": dropped_turns,
        "dropped_events": dropped_events,
        "feedback_clipped": clipped,
    }
