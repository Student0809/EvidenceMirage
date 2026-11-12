#!/usr/bin/env python3
"""Offline, exhaustive strategy/example overlap audit; never executes payloads.

Reproduce with the system Python from the project root. Uses only the standard
library and the repository's lossless Markdown reader. Similarity scores are
retrieval aids, not semantic labels or causal estimates.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.vendor.a2_converter import convert_document, restore_markdown


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normal(value):
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def shingles(value, n=5):
    words = re.findall(r"[\w@./:+-]+", normal(value))
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def scores(a, b):
    shared = len(a & b)
    return (round(shared / len(a | b), 6) if a or b else 0.0,
            round(shared / min(len(a), len(b)), 6) if a and b else 0.0)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_csv(path, data):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data[0]))
        writer.writeheader()
        for item in data:
            writer.writerow({key: ";".join(value) if isinstance(value, list) else value
                             for key, value in item.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/library_overlap_20261102")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    dataset_path = ROOT / "data/agentdojo_a2v2_test30_20261030/cases.jsonl"
    reference_path = dataset_path.parent / "reference.json"
    compact_path = ROOT / "strategy_library_compact/experiences.jsonl"
    a2_path = ROOT / "strategy_library_a2/library.json"
    tasks = rows(dataset_path)
    reference = read(reference_path)
    source = Path(reference["reference_run"])
    compact = {item["experience_id"]: item for item in rows(compact_path)}
    a2 = read(a2_path)
    assert len(tasks) == 30 and len({item["case_id"] for item in tasks}) == 30
    input_hashes = {str(path.relative_to(ROOT)): sha(path) for path in
                    (dataset_path, reference_path, compact_path, a2_path,
                     Path(__file__), ROOT / "src/vendor/a2_converter.py",
                     ROOT / "strategy_library/contexts/manifest.json")}
    examples = []
    strategy_rows = []
    for path in sorted((ROOT / "strategy_library").glob("*_attack_strategy_*.md")):
        text = path.read_bytes().decode()
        document = convert_document(text, path.name)
        assert restore_markdown(document) == text
        assert a2["source_sha256"][str(path.relative_to(ROOT))] == sha(path)
        original = source / "strategies" / path.name
        assert sha(original) == sha(path), f"Historical library differs: {path}"
        input_hashes[str(path.relative_to(ROOT))] = sha(path)
        input_hashes[str(original)] = sha(original)
        strategy_rows.append(dict(strategy_id=path.stem,
                                  example_count=len(document["examples"]),
                                  sha256=sha(path), original_snapshot_equal=True))
        for item in document["examples"]:
            eid = f"{path.stem}::example-{item['number']:03d}"
            old = compact[f"{path.stem}#ex{item['number']}"]
            # Compact is an older context snapshot. User task / goal / payload
            # identity is verified separately rather than assumed from IDs.
            assert old["observable_state"]["user_task"] == item["user_task"]
            assert old["observable_state"]["injection_goal"] == item["injection_goal"]
            assert old["winning_injection_text"] == item["winning_injection_text"]
            inline = item["placeholder_context"] or ""
            links = sorted(set(re.findall(
                r"strategy_library/contexts/[^`\s)]+\.json",
                inline + " " + (item["placeholder_context_reference"] or ""))))
            assert len(links) <= 1
            augmented = inline
            if links:
                linked = ROOT / links[0]
                context_record = read(linked)
                assert item["benchmark_case"] == {
                    key: context_record[name] for key, name in
                    (("suite", "suite_name"), ("user_task_id", "user_task_id"),
                     ("injection_task_id", "injection_task_id"))}
                augmented = context_record["context_with_placeholder"]
                assert hashlib.sha256(augmented.encode()).hexdigest() == context_record["context_sha256"]
                input_hashes[links[0]] = sha(linked)
            case = item["benchmark_case"] or {}
            record = dict(example_id=eid, strategy_id=path.stem,
                          dataset=old["dataset"], source=str(path.relative_to(ROOT)),
                          source_line=item["source_line"], number=item["number"],
                          suite=case.get("suite"), user_task_id=case.get("user_task_id"),
                          injection_task_id=case.get("injection_task_id"),
                          user_task=item["user_task"], injection_goal=item["injection_goal"],
                          winning_payload_sha256=hashlib.sha256(
                              (item["winning_injection_text"] or "").encode()).hexdigest(),
                          inline_context=inline, augmented_context=augmented,
                          compact_context=old["observable_state"]["placeholder_context"] or "",
                          context_reference=links[0] if links else None)
            record["task_shingles"] = shingles(record["user_task"])
            record["context_shingles"] = shingles(augmented)
            examples.append(record)
    assert len(examples) == len(a2["cases"]) == len(compact) == 100
    assert {item["example_id"] for item in examples} == {item["case_id"] for item in a2["cases"]}
    pairs = []
    summaries = []
    for task in tasks:
        sample_path = source / "samples" / f"{int(task['case_id'][1:]):03d}.json"
        sample = read(sample_path)
        assert all(task[key] == sample[key] for key in
                   ("suite_name", "user_task_id", "injection_task_id", "step_index",
                    "user_task_prompt", "injection_task_goal", "context_with_placeholder"))
        input_hashes[str(sample_path)] = sha(sample_path)
        route = set(sample["strategy_ids"])
        first_hit = next((attempt["iter"] + 1 for attempt in sample["history"]
                          if attempt.get("security") is True), None)
        current = []
        task_words, context_words = shingles(task["user_task_prompt"]), shingles(task["context_with_placeholder"])
        for ex in examples:
            same_suite = ex["dataset"] == "agentdojo" and ex["suite"] == task["suite_name"]
            same_base = same_suite and ex["user_task_id"] == task["user_task_id"]
            same_goal = same_suite and ex["injection_task_id"] == task["injection_task_id"]
            task_jaccard, task_contain = scores(task_words, ex["task_shingles"])
            context_jaccard, context_contain = scores(context_words, ex["context_shingles"])
            pair = dict(case_id=task["case_id"], example_id=ex["example_id"],
                        source=ex["source"], source_line=ex["source_line"],
                        strategy_routed=ex["strategy_id"] in route,
                        same_base_task=same_base, same_goal_id=same_goal,
                        same_task_goal=same_base and same_goal,
                        task_text_equal=normal(task["user_task_prompt"]) == normal(ex["user_task"]),
                        goal_text_equal=normal(task["injection_task_goal"]) == normal(ex["injection_goal"]),
                        compact_context_equal=normal(task["context_with_placeholder"]) == normal(ex["compact_context"]),
                        inline_context_equal=normal(task["context_with_placeholder"]) == normal(ex["inline_context"]),
                        augmented_context_equal=normal(task["context_with_placeholder"]) == normal(ex["augmented_context"]),
                        context_from_link=bool(ex["context_reference"]),
                        task_5gram_jaccard=task_jaccard, task_5gram_min_containment=task_contain,
                        context_5gram_jaccard=context_jaccard, context_5gram_min_containment=context_contain)
            current.append(pair)
            pairs.append(pair)
        summary = dict(case_id=task["case_id"], base_task_id=task["base_task_id"],
                       injection_task_id=task["injection_task_id"],
                       original_success=first_hit is not None, first_success_attempt=first_hit,
                       routed_strategies=sample["strategy_ids"])
        for key in ("same_base_task", "same_goal_id", "same_task_goal", "task_text_equal",
                    "goal_text_equal", "compact_context_equal", "inline_context_equal",
                    "augmented_context_equal"):
            summary[key] = [pair["example_id"] for pair in current if pair[key]]
            summary["routed_" + key] = [pair["example_id"] for pair in current if pair[key] and pair["strategy_routed"]]
        summary["goal_and_inline_context"] = [pair["example_id"] for pair in current
                                             if pair["same_goal_id"] and pair["inline_context_equal"]]
        summary["task_goal_inline_context"] = [pair["example_id"] for pair in current
                                              if pair["same_task_goal"] and pair["inline_context_equal"]]
        summary["any_original_dimension"] = bool(summary["same_base_task"] or summary["same_goal_id"]
                                                  or summary["compact_context_equal"])
        summary["any_augmented_dimension"] = bool(summary["same_base_task"] or summary["same_goal_id"]
                                                   or summary["augmented_context_equal"])
        summaries.append(summary)
    assert len(pairs) == 3000
    groups = {}
    for key in ("same_base_task", "same_goal_id", "same_task_goal", "compact_context_equal",
                "inline_context_equal", "augmented_context_equal", "goal_and_inline_context",
                "task_goal_inline_context", "any_original_dimension", "any_augmented_dimension"):
        matching = [item for item in summaries if item[key]]
        other = [item for item in summaries if not item[key]]
        groups[key] = dict(case_count=len(matching), case_ids=[item["case_id"] for item in matching],
                           successes=sum(item["original_success"] for item in matching),
                           remainder_count=len(other),
                           remainder_successes=sum(item["original_success"] for item in other),
                           interpretation="Descriptive only; groups overlap; no causal attribution.")
    stats = dict(dataset=str(dataset_path.relative_to(ROOT)), example_count=len(examples),
                 strategy_count=len(strategy_rows),
                 example_counts_by_dataset=dict(Counter(ex["dataset"] for ex in examples)),
                 pair_count=len(pairs), original_successes=sum(item["original_success"] for item in summaries),
                 groups=groups,
                 counts_are_normalized_exact="Whitespace collapsed and case folded; IDs require same benchmark/suite.",
                 context_caveat="Compact retains an earlier inline-context archive; 34 context fields differ from the current Markdown/original-run snapshots. Linked contexts are current-dataset reconstructions with unverified historical byte identity; report separately.",
                 causal_caveat="No library-removal counterfactual. Original run also enables rolling intra-dataset experience.",
                 input_sha256=input_hashes)
    write_json(out / "quantitative_summary.json", stats)
    write_json(out / "case_overlaps.json", summaries)
    with (out / "all_3000_pairs.jsonl").open("w") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair, ensure_ascii=False) + "\n")
    write_csv(out / "case_overlaps.csv", summaries)
    write_csv(out / "strategy_inventory.csv", strategy_rows)
    inventory = []
    for ex in examples:
        related = [pair for pair in pairs if pair["example_id"] == ex["example_id"]]
        inventory.append({key: value for key, value in ex.items()
                          if key not in {"task_shingles", "context_shingles", "inline_context", "augmented_context", "compact_context"}} |
                         {key: [pair["case_id"] for pair in related if pair[key]] for key in
                          ("same_base_task", "same_goal_id", "compact_context_equal", "augmented_context_equal")})
    write_json(out / "all_100_examples.json", inventory)
    print(json.dumps({"examples": len(examples), "pairs": len(pairs), "groups": groups},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
