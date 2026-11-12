#!/usr/bin/env python3
"""Restructure the verbose Markdown strategy library into a compact two-layer
form a small attacker model can actually consume.

Source of truth is ``strategy_library_json/`` (the faithful, content-preserving
JSON export of ``strategy_library/``); this builder never parses the raw
Markdown and never touches either source directory. It only *reorganizes*
already-extracted fields: nothing here rewrites, strengthens, or invents a
strategy claim. Every text field is copied verbatim from the JSON export.

Two output layers (see README.md for the rationale):

  Layer 1  cards/<strategy_id>.yaml   one compact "strategy card" per mechanism
                                      file. Small enough to hold several in a
                                      small model's context; the routing /
                                      thinking-strategy object.
  Layer 2  experiences.jsonl          one record per documented hit. The RAG
                                      retrieval corpus of concrete precedents.

Both layers point at each other by id; neither inlines the other. Stdlib +
PyYAML only. Re-runnable: overwrites its own outputs, never the sources.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
JSON_DIR = ROOT / "strategy_library_json"
# Target-model tokens use hyphens, the mechanism/target separator is the final
# underscore, so a single rsplit reliably splits "<mechanism>_<target>".
KNOWN_DATASETS = ("agentdojo", "ipi_arena", "injecagent")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean(text: str | None) -> str:
    """Strip trailing whitespace and a trailing Markdown section rule (``---``).

    The JSON export attaches the source's section-separator ``---`` to the last
    parsed item of each section; it carries no strategy content, so drop it.
    """
    lines = (text or "").rstrip().splitlines()
    while lines and lines[-1].strip() in ("---", "***", "___", ""):
        lines.pop()
    return "\n".join(lines).strip()


def first_sentence(markdown: str) -> str:
    text = clean(markdown).replace("\n", " ")
    if not text:
        return ""
    # Split on the first sentence terminator followed by a space; keep the period.
    for i in range(len(text) - 1):
        if text[i] in ".!?" and text[i + 1] == " ":
            return text[: i + 1].strip()
    return text.strip()


def group_key(dataset: str, suite: str | None, user_task_id: str | None) -> str:
    """Canonical (dataset, suite, user_task) key, matching strategy_research's
    ``schema.group_id`` so experiences join a research manifest's group roles."""
    return json.dumps([dataset, str(suite), str(user_task_id)], ensure_ascii=False, separators=(",", ":"))


def load_split_roles(manifest_paths: list[str]) -> dict[str, str]:
    """Merge {group_key: role} maps from one or more research manifests."""
    roles: dict[str, str] = {}
    for path in manifest_paths:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, role in manifest.get("groups", {}).items():
            if key in roles and roles[key] != role:
                raise ValueError(f"Conflicting split role for {key} across manifests")
            roles[key] = role
    return roles


def split_id(strategy_id: str) -> tuple[str, str]:
    """Return (dataset, mechanism). Dataset prefix is stripped, then the final
    underscore separates the mechanism from the (hyphenated) target-model tag."""
    dataset = next((d for d in KNOWN_DATASETS if strategy_id.startswith(d + "_")), None)
    if dataset is None:
        raise ValueError(f"Unknown dataset prefix in id: {strategy_id}")
    stem = strategy_id[len(dataset) + 1:]
    stem = stem[len("attack_strategy_"):] if stem.startswith("attack_strategy_") else stem
    mechanism = stem.rsplit("_", 1)[0] if "_" in stem else stem
    return dataset, mechanism


def items(section: dict) -> list[dict]:
    """Normalize a section's parsed bullets to {label, text} pairs, verbatim."""
    out = []
    for item in section.get("items") or []:
        label = (item.get("label") or "").strip()
        text = clean(item.get("text"))
        out.append({"label": label, "text": text} if label else {"text": text})
    return out


def build_experiences(strategy_id: str, mechanism: str, dataset: str, doc: dict,
                      source_json: str, source_sha: str, split_roles: dict[str, str]) -> list[dict]:
    records = []
    for ex in doc.get("examples", []):
        case = ex.get("benchmark_case") or {}
        number = ex.get("number")
        gkey = group_key(dataset, case.get("suite"), case.get("user_task_id"))
        record = {
            "experience_id": f"{strategy_id}#ex{number}",
            "strategy_id": strategy_id,
            "mechanism": mechanism,
            "dataset": dataset,
            "target_model": ex.get("target_model"),
            "suite": case.get("suite"),
            "user_task_id": case.get("user_task_id"),
            "injection_task_id": case.get("injection_task_id"),
            "group_id": gkey,
            "split_role": split_roles.get(gkey, "unassigned"),
            "observable_state": {
                "user_task": ex.get("user_task"),
                "injection_goal": ex.get("injection_goal"),
                "placeholder_context": ex.get("placeholder_context"),
                "placeholder_context_reference": ex.get("placeholder_context_reference"),
            },
            "winning_injection_text": ex.get("winning_injection_text"),
            "result": ex.get("result"),
            "why_it_landed": ex.get("why_it_worked"),
            "reference_only": ex.get("placeholder_context") is None,
            "provenance": {
                "source_json": source_json,
                "source_line": ex.get("source_line"),
                "source_sha256": source_sha,
            },
        }
        records.append(record)
    return records


def build_card(strategy_id: str, mechanism: str, dataset: str, doc: dict,
               example_refs: list[str], source_json: str, source_sha: str,
               experiences: list[dict]) -> dict:
    role_dist: dict[str, int] = {}
    for exp in experiences:
        role_dist[exp["split_role"]] = role_dist.get(exp["split_role"], 0) + 1
    return {
        "strategy_id": strategy_id,
        "dataset": dataset,
        "mechanism": mechanism,
        "title": doc.get("title"),
        "one_line_summary": first_sentence(doc.get("introduction_markdown", "")),
        "introduction": clean(doc.get("introduction_markdown")),
        "core_moves": [{"name": (s.get("markdown", "").split("**")[1].strip()
                                 if "**" in (s.get("markdown") or "") else s.get("name") or ""),
                        "text": clean(s.get("text"))}
                       for s in doc.get("procedure", {}).get("steps", [])],
        "decision_summary": clean(doc.get("mechanism_distinction", {}).get("markdown")),
        "preconditions": items(doc.get("scope", {}).get("user_and_injected_tasks", {})),
        "target_support": items(doc.get("scope", {}).get("target_models", {})),
        "observable_failure_signals": items(doc.get("failure_conditions", {})),
        "initialization_guidance": items(doc.get("initialization_guidance", {})),
        "example_refs": example_refs,
        "support_count": len(example_refs),
        # A card spans many task groups, so split lives per-experience; this is the
        # distribution of this card's experiences across research-manifest roles.
        "split_role_distribution": dict(sorted(role_dist.items())),
        "provenance": {
            "source_markdown": doc.get("source", {}).get("filename"),
            "source_json": source_json,
            "source_sha256": source_sha,
            "reorganized_from": "strategy_library_json (verbatim field copy)",
        },
    }


def load_research_experiences(dest: Path) -> dict[str, list[dict]]:
    """Group ingested (non-baseline) experiences by strategy_id, if present.

    These come from ``ingest_research.py`` (axis-A expansion) and live in a
    separate append-only file this builder never overwrites. They are folded into
    each card's example_refs / distribution so cards view all evidence, while the
    lossless-from-JSON ``experiences.jsonl`` and ``--check`` stay unaffected.
    """
    path = dest / "experiences_research.jsonl"
    grouped: dict[str, list[dict]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                grouped.setdefault(rec["strategy_id"], []).append(rec)
    return grouped


def build(dest: Path, manifest_paths: list[str] | None = None) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "cards").mkdir(exist_ok=True)
    split_roles = load_split_roles(manifest_paths or [])
    research = load_research_experiences(dest)
    index = json.loads((JSON_DIR / "index.json").read_text(encoding="utf-8"))
    entries = [e for e in index["entries"] if e["kind"] == "strategy"]
    all_experiences: list[dict] = []
    card_summaries: list[dict] = []
    mechanisms: set[str] = set()

    for entry in sorted(entries, key=lambda e: e["id"]):
        source_json = f"strategy_library_json/{entry['file']}"
        source_sha = entry["source_sha256"]
        doc = json.loads((JSON_DIR / entry["file"]).read_text(encoding="utf-8"))
        strategy_id = doc["id"]
        dataset, mechanism = split_id(strategy_id)
        mechanisms.add(mechanism)

        experiences = build_experiences(strategy_id, mechanism, dataset, doc, source_json, source_sha, split_roles)
        all_experiences.extend(experiences)
        # Fold in any ingested research evidence for this mechanism (kept separate on disk).
        card_experiences = experiences + research.get(strategy_id, [])
        example_refs = [e["experience_id"] for e in card_experiences]

        card = build_card(strategy_id, mechanism, dataset, doc, example_refs, source_json, source_sha,
                          card_experiences)
        rendered = yaml.safe_dump(card, allow_unicode=True, sort_keys=False, width=100)
        card["token_length_chars"] = len(rendered)
        rendered = yaml.safe_dump(card, allow_unicode=True, sort_keys=False, width=100)
        (dest / "cards" / f"{strategy_id}.yaml").write_text(rendered, encoding="utf-8")

        card_summaries.append({
            "strategy_id": strategy_id, "dataset": dataset, "mechanism": mechanism,
            "support_count": len(example_refs), "card_chars": len(rendered),
            "source_sha256": source_sha,
        })

    with (dest / "experiences.jsonl").open("w", encoding="utf-8") as fh:
        for record in all_experiences:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "schema_version": 1,
        "source": "strategy_library_json",
        "card_count": len(card_summaries),
        "mechanism_count": len(mechanisms),
        "mechanisms": sorted(mechanisms),
        "experience_count": len(all_experiences),
        "research_experience_count": sum(len(v) for v in research.values()),
        "reference_only_experiences": sum(e["reference_only"] for e in all_experiences),
        "target_models": sorted({e["target_model"] for e in all_experiences if e["target_model"]}),
        "datasets": sorted({c["dataset"] for c in card_summaries}),
        "split_manifests": list(manifest_paths or []),
        "split_role_counts": {role: sum(e["split_role"] == role for e in all_experiences)
                              for role in sorted({e["split_role"] for e in all_experiences})},
        "cards": card_summaries,
    }
    (dest / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def check(dest: Path) -> dict:
    """Verify the built library is a faithful, lossless reorganization of the JSON.

    Confirms every experience's verbatim payload fields equal the source JSON
    and that no documented hit was dropped. Raises on any mismatch.
    """
    index = json.loads((JSON_DIR / "index.json").read_text(encoding="utf-8"))
    entries = {e["id"]: e for e in index["entries"] if e["kind"] == "strategy"}
    records = [json.loads(line) for line in
               (dest / "experiences.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    by_strategy: dict[str, dict[int, dict]] = {}
    for rec in records:
        by_strategy.setdefault(rec["strategy_id"], {})[int(rec["experience_id"].split("#ex")[1])] = rec
    checked = 0
    for sid, entry in entries.items():
        doc = json.loads((JSON_DIR / entry["file"]).read_text(encoding="utf-8"))
        card = yaml.safe_load((dest / "cards" / f"{sid}.yaml").read_text(encoding="utf-8"))
        if len(card["core_moves"]) != len(doc.get("procedure", {}).get("steps", [])):
            raise ValueError(f"{sid}: core_moves count differs from procedure steps")
        recs = by_strategy.get(sid, {})
        if len(recs) != entry["example_count"]:
            raise ValueError(f"{sid}: {len(recs)} experiences vs {entry['example_count']} documented hits")
        for ex in doc["examples"]:
            rec = recs[ex["number"]]
            top_fields = {"winning_injection_text": "winning_injection_text",
                          "result": "result", "why_it_worked": "why_it_landed"}
            state_fields = {"user_task": "user_task", "injection_goal": "injection_goal",
                            "placeholder_context": "placeholder_context"}
            for src_key, dst_key in top_fields.items():
                if rec.get(dst_key) != ex.get(src_key):
                    raise ValueError(f"{sid}#ex{ex['number']}: field {src_key} not verbatim")
            for src_key, dst_key in state_fields.items():
                if rec["observable_state"].get(dst_key) != ex.get(src_key):
                    raise ValueError(f"{sid}#ex{ex['number']}: observable_state.{dst_key} not verbatim")
            checked += 1
    return {"strategies_checked": len(entries), "experiences_checked": checked, "status": "faithful"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default=str(ROOT / "strategy_library_compact"),
                        help="Output directory for the compact two-layer library")
    parser.add_argument("--check", action="store_true",
                        help="Verify an existing build is a faithful reorganization of the JSON source")
    parser.add_argument("--manifest", action="append", default=[],
                        help="Research manifest.json to assign split_role from (repeatable, one per dataset)")
    args = parser.parse_args(argv)
    if args.check:
        print(json.dumps(check(Path(args.dest)), ensure_ascii=False, indent=2))
        return 0
    manifest = build(Path(args.dest), args.manifest)
    print(json.dumps({k: v for k, v in manifest.items() if k != "cards"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
