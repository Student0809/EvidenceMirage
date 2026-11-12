"""Read-only library evidence audit. No models, network, payload output, or DB writes.

Usage: python strategy_library_compact/audit_metadata.py > /tmp/library-audit.json
Counts describe archived records, not routing frequency or measured success rates.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def counts(rows: list[dict], key: str) -> dict:
    return dict(sorted(Counter(str(row.get(key)) for row in rows).items()))


def audit(root: Path) -> dict:
    directory = root / "strategy_library_compact"
    index_path = directory / "index.json"
    baseline_path = directory / "experiences.jsonl"
    index = json.loads(index_path.read_text())
    baseline = read_rows(baseline_path)
    research_path = directory / "experiences_research.jsonl"
    research = read_rows(research_path) if research_path.exists() else []
    source_paths = [index_path, baseline_path]
    if research_path.exists():
        source_paths.append(research_path)
    manifests = {}
    for relative in index.get("split_manifests", []):
        path = root / relative
        source_paths.append(path)
        for group, role in json.loads(path.read_text())["groups"].items():
            if group in manifests and manifests[group] != role:
                raise ValueError(f"Conflicting split for {group}")
            manifests[group] = role

    rows = baseline + research
    ids = Counter(row["experience_id"] for row in rows)
    indexed_ids = {card["strategy_id"] for card in index["cards"]}
    disk_ids = {path.stem for path in (directory / "cards").glob("*.yaml")}
    cards = []
    for card in index["cards"]:
        path = directory / "cards" / (card["strategy_id"] + ".yaml")
        source_paths.append(path)
        relevant = [row for row in rows if row["strategy_id"] == card["strategy_id"]]
        cards.append({
            "strategy_id": card["strategy_id"], "mechanism": card["mechanism"],
            "actual_characters": len(path.read_text()),
            "archived_records": len(relevant),
            "unique_groups": len({row["group_id"] for row in relevant}),
            "split_records": counts(relevant, "split_role"),
        })
    by_mechanism = []
    for mechanism in sorted({row["mechanism"] for row in rows}):
        relevant = [row for row in rows if row["mechanism"] == mechanism]
        discovery = [row for row in relevant if row["split_role"] == "discovery"]
        by_mechanism.append({
            "mechanism": mechanism, "archived_records": len(relevant),
            "unique_groups": len({row["group_id"] for row in relevant}),
            "discovery_records": len(discovery),
            "discovery_groups": len({row["group_id"] for row in discovery}),
            "targets": counts(relevant, "target_model"),
            "datasets": counts(relevant, "dataset"),
        })
    by_split = {}
    for role in sorted({row["split_role"] for row in rows}):
        relevant = [row for row in rows if row["split_role"] == role]
        by_split[role] = {
            "records": len(relevant),
            "unique_groups": len({row["group_id"] for row in relevant}),
            "reference_only_records": sum(bool(row.get("reference_only")) for row in relevant),
        }
    counts_by_mechanism = Counter(row["mechanism"] for row in rows)
    return {
        "schema_version": 1,
        "scope": "Archived library metadata; no causal, routing, or efficacy inference",
        "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(set(source_paths))},
        "summary": {
            "indexed_cards": len(cards), "mechanisms": len(by_mechanism),
            "baseline_records": len(baseline), "research_records": len(research),
            "unique_groups": len({row["group_id"] for row in rows}),
            "top_two_record_share": sum(n for _, n in counts_by_mechanism.most_common(2)) / len(rows) if rows else None,
            "target_records": counts(rows, "target_model"), "splits": by_split,
        },
        "integrity": {
            "duplicate_experience_ids": sorted(key for key, n in ids.items() if n > 1),
            "split_mismatches": [row["experience_id"] for row in rows
                                 if manifests.get(row["group_id"]) != row.get("split_role")],
            "cards_on_disk_not_indexed": sorted(disk_ids - indexed_ids),
            "experience_strategies_not_indexed": sorted({row["strategy_id"] for row in rows} - indexed_ids),
            "index_baseline_count_matches": index.get("experience_count") == len(baseline),
            "index_research_count_matches": index.get("research_experience_count") == len(research),
        },
        "mechanisms": by_mechanism, "cards": cards,
        "limitations": [
            "Archived wins do not reveal how often a router selected a mechanism or how often it failed.",
            "Different records and mechanisms may share one task group; group counts are not additive.",
            "Matching split labels does not prove absence of semantic overlap or prior exposure in card text.",
            "Characters are not tokenizer tokens; reference_only=false does not certify sample completeness.",
            "No student suitability or incremental performance gain is measured by this audit.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(audit(args.root.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
