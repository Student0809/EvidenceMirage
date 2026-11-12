"""Original PIMiner experience helpers, extracted without its CLI imports.

Function bodies are verbatim from data/original/piminer_code/
iterative_attack_claude_code.py. The concurrent run-local wrapper lives in
src.attacker.experience; its private temporary files avoid the original append
helper's check-before-lock race on shared state.
"""
import fcntl
import json
from pathlib import Path
from typing import Any, Dict

INTRA_EXPERIENCE_FILE = "intra_dataset_experience.jsonl"


def _intra_experience_path(run_dir: Path) -> Path:
    return run_dir / INTRA_EXPERIENCE_FILE


def _clip_context(ctx: str, window: int = 220) -> str:
    """A short, deterministic clip of the polluted context, centered on the
    `{INJECTION:...}` slot (where the attacker text lands) so later samples see
    the surface shape without inlining the full context."""
    if not ctx:
        return ""
    i = ctx.find("{INJECTION")
    if i == -1:
        clip = ctx[: window * 2]
        return clip + (" …[clipped]" if len(ctx) > window * 2 else "")
    lo, hi = max(0, i - window), min(len(ctx), i + window)
    clip = ctx[lo:hi]
    if lo > 0:
        clip = "…" + clip
    if hi < len(ctx):
        clip = clip + "…"
    return clip


def _append_intra_experience(run_dir: Path, sample: Dict[str, Any]) -> None:
    """Append one deterministic experience entry for a just-terminal sample.
    Idempotent: no-op if this sample_index is already logged."""
    path = _intra_experience_path(run_dir)
    idx = sample["sample_index"]
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("sample_index") == idx:
                    return
            except json.JSONDecodeError:
                continue
    history = sample.get("history", [])
    win = next((h for h in history if h.get("security")), None)
    entry = {
        "sample_index": idx,
        "suite_name": sample.get("suite_name"),
        # Real strings (semantic content the attacker reasons over). The id
        # triple is kept too, but only as compact audit metadata — it carries no
        # meaning to the attacker and does not generalise across benchmarks.
        "user_task": sample.get("user_task_prompt"),
        "injection_goal": sample.get("injection_task_goal"),
        "context_clip": _clip_context(sample.get("context_with_placeholder") or ""),
        "user_task_id": sample.get("user_task_id"),
        "injection_task_id": sample.get("injection_task_id"),
        "outcome": sample.get("status"),
        "success": sample.get("status") == "hit",
        "iters_used": len(history),
        "routed_strategy_ids": sample.get("strategy_ids", []),
        "winning_iter": win["iter"] if win else None,
        "winning_injection": win["injection"] if win else None,
        # The terminal iter's injection — on a miss this is the failed attempt
        # that `final_analysis` describes; on a hit it equals winning_injection.
        "final_injection": (history[-1].get("injection") if history else None),
        "final_analysis": (history[-1].get("analysis") if history else "") or "",
    }
    # Exclusive lock: under wave-parallel attacks, up to N sessions may append
    # concurrently (always for distinct sample_index). flock serializes the
    # whole-line writes so entries never interleave.
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _format_intra_experience_block(run_dir: Path, before_index: int,
                                   max_chars: int = 20000) -> str:
    """Render experience entries for samples with index < before_index as a
    controlled prompt block (most-recent-first truncation to a char budget)."""
    path = _intra_experience_path(run_dir)
    if not path.exists():
        return ""
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("sample_index", 1 << 30) < before_index:
            entries.append(e)
    if not entries:
        return ""
    entries.sort(key=lambda e: e["sample_index"])
    blocks = []
    for e in entries:
        oc = e.get("outcome")
        b = [
            f"### Sample {e['sample_index']} ({e.get('suite_name')}) "
            f"→ {oc} ({e.get('iters_used')} iters)",
            f"- user task: {(e.get('user_task') or '').strip()}",
            f"- injection goal: {(e.get('injection_goal') or '').strip()}",
            f"- routed strategies: {', '.join(e.get('routed_strategy_ids') or []) or '(none)'}",
        ]
        cc = (e.get("context_clip") or "").strip()
        if cc:
            b.append("- placeholder context (clipped, `{INJECTION:...}` = where text lands):")
            b.append("```\n" + cc + "\n```")
        if oc == "hit" and e.get("winning_injection"):
            b.append(f"- WINNING injection (iter {e.get('winning_iter')}), verbatim:")
            b.append("```\n" + e["winning_injection"] + "\n```")
        elif oc == "miss" and e.get("final_injection"):
            b.append("- final (FAILED) injection, verbatim — this corresponds to the analysis below:")
            b.append("```\n" + e["final_injection"] + "\n```")
        fa = (e.get("final_analysis") or "").strip()
        if fa:
            b.append(f"- attacker's final-iter analysis: {fa}")
        blocks.append("\n".join(b))
    out, total = [], 0
    for b in reversed(blocks):
        if total + len(b) > max_chars and out:
            out.append(f"... [older experience truncated to fit {max_chars} chars] ...")
            break
        out.append(b)
        total += len(b)
    out.reverse()
    header = (
        f"## Intra-dataset experience (earlier samples 0..{before_index - 1} in THIS "
        "dataset — explicit cross-sample memory; reuse what worked, avoid what failed)\n"
    )
    return header + "\n\n".join(out)
