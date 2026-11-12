#!/usr/bin/env python3
"""Merge new evidence from a ``strategy_research`` run into the compact library.

``build.py`` only ever reads ``strategy_library_json/`` — the lossless baseline.
This tool is the *other* inbound path: it turns the live products of an axis-A
expansion run (new target hits, newly verified mechanisms) into compact-library
records, so the small attacker model can use them.

Two commands:

  experiences   Ingest hit episodes from a research store as new experience
                records under an EXISTING card (A1, 补目标覆盖). One record per
                hit; linked to a caller-named strategy_id so the mechanism is
                explicit, never guessed from strategy text.

  card          Ingest a newly VERIFIED mechanism (from ``export-library`` JSON)
                as a new compact card (A2, 补机制). Prose comes from the
                validated candidate's proposal.

Outputs, never touching the baseline:
  experiences_research.jsonl   append-only, deduped by experience_id
  cards/<id>.yaml              new-mechanism cards only (baseline cards belong
                               to build.py, which folds these records into their
                               example_refs on the next rebuild)

Discipline enforced here (mirrors EXPANSION_AXIS_A.md): only discovery-group,
black-box, non-synthetic, actually-successful episodes become experiences;
test / development / validation groups are refused so mined evidence can never
leak into the held-out or selection pools.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategy_research.store import Store  # noqa: E402

from build import clean, first_sentence, group_key, load_split_roles, split_id  # noqa: E402

DEFAULT_DEST = ROOT / "strategy_library_compact"
RESEARCH_EXPERIENCES = "experiences_research.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def episode_hit(episode: dict) -> dict | None:
    """Return the first successful, error-free attempt, or None."""
    for attempt in episode.get("attempts", []):
        if attempt.get("success") and not attempt.get("error"):
            return attempt
    return None


def experience_from_episode(strategy_id: str, mechanism: str, episode_id: str, episode: dict,
                            attempt: dict, split_role: str, db_path: str) -> dict:
    task = episode.get("task", {})
    short = episode_id.split("_")[-1][:12]
    return {
        "experience_id": f"{strategy_id}#r{short}",
        "strategy_id": strategy_id,
        "mechanism": mechanism,
        "dataset": episode.get("dataset"),
        "target_model": episode.get("target_model"),
        "suite": task.get("suite_name"),
        "user_task_id": task.get("user_task_id"),
        "injection_task_id": task.get("injection_task_id"),
        "group_id": episode.get("group_id"),
        "split_role": split_role,
        "observable_state": {
            "user_task": task.get("user_task_prompt"),
            "injection_goal": task.get("injection_task_goal"),
            "placeholder_context": task.get("context_with_placeholder"),
            "placeholder_context_reference": None,
        },
        "winning_injection_text": attempt.get("injection"),
        "result": attempt.get("feedback"),
        # The attacker's pre-submission analysis, not a post-hoc explanation.
        "why_it_landed": attempt.get("analysis"),
        "iter_at_hit": attempt.get("iter"),
        "reference_only": task.get("context_with_placeholder") is None,
        "provenance": {
            "source": "strategy_research",
            "db": db_path,
            "episode_id": episode_id,
            "trace_id": f"{episode_id}:{attempt.get('iter')}",
            "attacker_model": episode.get("attacker_model"),
        },
    }


def ingest_experiences(dest: Path, db_path: str, strategy_id: str, manifest_paths: list[str],
                       episode_ids: list[str] | None, allow_privileged: bool) -> dict:
    _, mechanism = split_id(strategy_id)  # also validates the id shape
    split_roles = load_split_roles(manifest_paths)
    if not split_roles:
        raise ValueError("--manifest is required so ingested experiences carry a verified discovery split")
    store = Store(db_path)
    try:
        if episode_ids:
            episodes = [(eid, store.get(eid, "episode")) for eid in episode_ids]
        else:
            episodes = store.items("episode")
    finally:
        store.close()

    out_path = dest / RESEARCH_EXPERIENCES
    seen = {rec["experience_id"] for rec in read_jsonl(out_path)}
    accepted, skipped = [], []
    for episode_id, episode in episodes:
        reason = None
        attempt = episode_hit(episode)
        role = split_roles.get(episode.get("group_id"), "unassigned")
        if attempt is None:
            reason = "no_successful_attempt"
        elif episode.get("synthetic"):
            reason = "synthetic"
        elif episode.get("visibility") != "black_box" and not allow_privileged:
            reason = "privileged"
        elif role != "discovery":
            reason = f"non_discovery_group({role})"
        if reason:
            skipped.append({"episode_id": episode_id, "reason": reason})
            continue
        record = experience_from_episode(strategy_id, mechanism, episode_id, episode, attempt, role, db_path)
        if record["experience_id"] in seen:
            skipped.append({"episode_id": episode_id, "reason": "already_ingested"})
            continue
        seen.add(record["experience_id"])
        accepted.append(record)

    append_jsonl(out_path, accepted)
    return {"strategy_id": strategy_id, "mechanism": mechanism, "ingested": len(accepted),
            "skipped": skipped, "output": str(out_path)}


def card_from_proposal(strategy_id: str, dataset: str, entry: dict) -> dict:
    proposal = entry.get("provenance", {}).get("proposal", {})
    mechanism = split_id(strategy_id)[1] if strategy_id else None
    moves = [{"name": step.split(":", 1)[0].strip() if ":" in step else f"move {i + 1}",
              "text": clean(step)} for i, step in enumerate(proposal.get("decision_steps", []))]
    return {
        "strategy_id": strategy_id,
        "dataset": dataset,
        "mechanism": mechanism,
        "title": proposal.get("title") or entry.get("title"),
        "one_line_summary": first_sentence(proposal.get("mechanism", "")),
        "introduction": clean(proposal.get("mechanism", "")),
        "core_moves": moves,
        "decision_summary": clean(proposal.get("distinction", "")),
        "preconditions": [{"text": clean(p)} for p in proposal.get("preconditions", [])],
        "target_support": [{"text": clean(t)} for t in proposal.get("transfer_predictions", [])],
        "observable_failure_signals": [{"text": clean(s)} for s in proposal.get("failure_signals", [])],
        "initialization_guidance": [],
        "example_refs": [],
        "support_count": 0,
        "split_role_distribution": {},
        "provenance": {
            "source": "strategy_research",
            "candidate_id": entry.get("candidate_id"),
            "report_id": entry.get("report_id"),
            "relation": entry.get("relation"),
            "nearest_strategy_id": entry.get("nearest_strategy_id"),
            "verified": True,
        },
    }


def ingest_card(dest: Path, export_path: str, dataset: str, candidate_ids: list[str] | None) -> dict:
    export = json.loads(Path(export_path).read_text(encoding="utf-8"))
    entries = export.get("strategies", [])
    if candidate_ids:
        entries = [e for e in entries if e.get("candidate_id") in set(candidate_ids)]
    if not entries:
        raise ValueError("No matching verified strategies in the export")
    (dest / "cards").mkdir(parents=True, exist_ok=True)
    written = []
    for entry in entries:
        cid = entry.get("candidate_id", "")
        title = entry.get("title", "")
        slug = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")[:48] or cid[:12]
        strategy_id = f"{dataset}_attack_strategy_{slug}_research"
        card = card_from_proposal(strategy_id, dataset, entry)
        path = dest / "cards" / f"{strategy_id}.yaml"
        rendered = yaml.safe_dump(card, allow_unicode=True, sort_keys=False, width=100)
        card["token_length_chars"] = len(rendered)
        path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
        written.append(str(path))
    return {"cards_written": written,
            "note": "Ingest hits for these mechanisms with the 'experiences' command; "
                    "run build.py to fold their refs into example_refs."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="Compact library directory")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("experiences", help="Ingest hit episodes as experiences under an existing card")
    exp.add_argument("--db", required=True, help="strategy_research SQLite store")
    exp.add_argument("--strategy-id", required=True, help="Existing compact card id the hits belong to")
    exp.add_argument("--manifest", action="append", default=[], required=True,
                     help="Research manifest.json for the discovery-split check (repeatable)")
    exp.add_argument("--episode-ids", nargs="+", help="Specific episodes; default scans all in the store")
    exp.add_argument("--allow-privileged", action="store_true", help="Permit non-black-box episodes")

    crd = sub.add_parser("card", help="Ingest a verified mechanism from export-library JSON as a new card")
    crd.add_argument("--export", required=True, help="strategy_research export-library output JSON")
    crd.add_argument("--dataset", required=True, choices=["agentdojo", "ipi_arena", "injecagent"])
    crd.add_argument("--candidate-ids", nargs="+", help="Subset of verified candidates; default all")

    args = parser.parse_args(argv)
    dest = Path(args.dest)
    if args.command == "experiences":
        result = ingest_experiences(dest, args.db, args.strategy_id, args.manifest,
                                    args.episode_ids, args.allow_privileged)
    else:
        result = ingest_card(dest, args.export, args.dataset, args.candidate_ids)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
