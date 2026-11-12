"""Verbatim original black-box final-output projection."""
import json
from typing import Any, List, Tuple


def format_final_output(text: str, calls: List[Tuple[str, str]]) -> str:
    """Render the agent's FINAL output for the black-box threat model: its final
    assistant text plus the final tool call(s) it emitted (name + args). This is
    the agent's externally-observable *action* — what a black-box observer sees —
    while the intermediate trajectory and all tool results stay hidden.

    `calls` is a list of (tool_name, args_str) for the final assistant turn only.
    """
    parts: List[str] = []
    t = (text or "").strip()
    parts.append(t if t else "(no assistant text)")
    for name, args in calls:
        parts.append(f"[tool_call] {name}({(args or '')[:1000]})")
    return "\n".join(parts)


def extract_final_output(messages: List[Any]) -> str:
    """Black-box final output for the agentdojo path: text + tool call(s) of the
    LAST assistant message only. Intermediate turns and tool results are excluded
    (a pure assistant message carries no tool_result blocks, so reusing the block
    logic below yields exactly the final text + [tool_call] lines)."""
    last = None
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "assistant":
            last = m
    if last is None:
        return format_final_output("", [])
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
    text = ""
    calls: List[Tuple[str, str]] = []
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    text += blk.get("content", "") or blk.get("text", "")
                elif blk.get("type") == "tool_use":
                    calls.append((blk.get("name"), json.dumps(blk.get("input") or {}, default=str)))
    # OpenAI-style assistant.tool_calls / agentdojo FunctionCall fallback.
    if isinstance(last, dict) and last.get("tool_calls"):
        for tc in last["tool_calls"]:
            fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", "")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            calls.append((fn, json.dumps(args, default=str)))
    return format_final_output(text, calls)

