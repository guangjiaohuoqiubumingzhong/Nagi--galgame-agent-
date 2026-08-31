"""Versioned opening selections. Never substitute archive order for story order."""

POLICY = "opening-story-v1"
LIMIT = 50


def selection_receipt(engine, entry, identifiers):
    return {
        "policy": POLICY,
        "engine": engine,
        "entry": entry,
        "limit": LIMIT,
        "selected_ids": list(identifiers),
    }


def unsupported(detail):
    return ValueError(f"无法安全确定开场剧情前 50 条：{detail}。请使用全文翻译或补充引擎入口适配。")
