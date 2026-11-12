# Compact two-layer strategy library (for the small-attacker研究)

The opt-in runtime now uses a separate, strictly bounded three-layer format in
[`strategy_runtime/`](../strategy_runtime), documented in
[the runtime guide](../docs/attacker_strategy_runtime_zh.md). This directory stays
the lossless archival view; its existing large YAML cards are not runtime menu cards.

This is a **restructuring** of the existing PIMiner strategy library into a form
a small attacker model (e.g. Qwen-8B) can actually consume. It does **not**
replace anything: `strategy_library/` (Markdown) and `strategy_library_json/`
(faithful JSON export) stay untouched as the source of truth and as baselines.

`build.py` reads only `strategy_library_json/` and reorganizes already-extracted
fields. It never rewrites, strengthens, or invents a strategy claim — every text
field is copied verbatim from the JSON export, and `--check` proves the copy is
lossless (all 100 documented hits, byte-for-byte).

## Why restructure

The Markdown library was written for a **large-LLM router + large-LLM attacker**
that reads a whole strategy file at PAIR-init. Each confirmed hit is appended
verbatim, so files grow to 11K–**369K** characters and mix three things a small
model needs to consume separately:

1. the abstract *recipe* (when to use this, what moves to make, how it fails),
2. the concrete *precedents* (100 winning injections with their contexts),
3. archival prose.

A Qwen-8B cannot hold a 369K-char file, and even if it could, the routing signal
(which strategy, why) is buried in narrative. So we split into two layers.

## The two layers

### Layer 1 — `cards/<strategy_id>.yaml` (10 files, one per mechanism file)

The compact **strategy card**: the routing / thinking-strategy object. Each card
holds the abstract recipe and *pointers* to its precedents, never the precedents
themselves. Fields:

| field | source (verbatim from JSON) | use |
| --- | --- | --- |
| `strategy_id`, `dataset`, `mechanism` | filename identity | id / grouping |
| `title`, `one_line_summary`, `introduction` | `title`, first sentence + full intro | quick routing read |
| `core_moves[]` | `procedure.steps` (name + text) | the abstract recipe |
| `decision_summary` | `mechanism_distinction` | when to pick this vs siblings |
| `preconditions[]` | `scope.user_and_injected_tasks` items | applicability gate |
| `target_support[]` | `scope.target_models` items | confirmed / likely / not-transfer |
| `observable_failure_signals[]` | `failure_conditions` items | read-and-repair signals |
| `initialization_guidance[]` | `initialization_guidance` items | iter-0 / escalation hints |
| `example_refs[]` | example ids | link into Layer 2 |
| `support_count`, `token_length_chars` | derived | selection economics |
| `provenance`, `split_group_id` | source hashes / null | audit; split assigned later |

The 369K `ipi_arena … fabricated_procedure_gate` file becomes a **61K** card
(16%); `forged_chat_turn` 176K → 34K (19%). The removed bulk is the 100 examples,
now in Layer 2.

### Layer 2 — `experiences.jsonl` (100 records)

The **feedback-experience corpus** — the RAG retrieval target. One record per
documented hit:

```jsonc
{
  "experience_id": "<strategy_id>#exN",
  "strategy_id", "mechanism", "dataset", "target_model",
  "suite", "user_task_id", "injection_task_id",
  "observable_state": { "user_task", "injection_goal",
                        "placeholder_context", "placeholder_context_reference" },
  "winning_injection_text",       // the payload that landed, verbatim
  "result", "why_it_landed",
  "reference_only": false,        // true for the 4 hits whose context is a pointer
  "provenance": { "source_json", "source_line", "source_sha256" }
}
```

`observable_state` is deliberately the *pre-attack* information (task + polluted
slot + goal) so it can be a retrieval key; the payload/result are the label.

## How this serves the three paper contributions

- **(3) small-LLM thinking-strategy library** — Layer 1 cards *are* that library:
  compact, structured, routable. Several fit in an 8B context at once.
- **(2) agent thinking method (CoT / RAG)** — retrieve top-k Layer 2 records by
  `observable_state` similarity to give the small model concrete precedents as a
  few-shot "thinking trajectory"; the card's `core_moves` + `decision_summary`
  seed the CoT.
- **(1) small model is enough given a good library** — the `(observable_state →
  chosen card + analysis + winning_injection_text)` tuples in Layer 2 are exactly
  the Decision-SFT training triples the research plan calls for.

## Relation to `strategy_research/`

The compact library is the natural object for the `strategy_research` pipeline:
Layer 1 cards can be registered as priors, and any *new* mechanism mined by
`propose → review → plan → report → activate` can be emitted in this same card
schema. See `research/small_attacker_research_plan_zh.md` for the split /
validation / SFT protocol.

## Commands

```bash
# (re)build from the JSON export
python strategy_library_compact/build.py

# assign split_role from research manifest(s) while rebuilding
python strategy_library_compact/build.py --manifest <manifest.json> [--manifest ...]

# verify the build is a faithful, lossless reorganization of the JSON
python strategy_library_compact/build.py --check
```

`build.py` is offline (stdlib + PyYAML) and never modifies the source directories.

To merge new evidence from a `strategy_research` expansion run (axis A), see
`ingest_research.py` and `EXPANSION_AXIS_A.md`: it appends ingested hits to
`experiences_research.jsonl` (append-only; `build.py` folds them into cards but
never overwrites them) and writes new-mechanism cards from verified candidates.

## Caveats

- This is a **restructuring**, not new evidence. Coverage is still the original
  7 mechanisms / 100 hits, mostly against `gpt-5-nano` on `agentdojo`/`ipi_arena`.
  Breadth expansion (more mechanisms, more targets) is the next step.
- `split_group_id` is `null`: train/dev/test grouping is assigned from a
  `strategy_research` manifest, not inferable from a strategy file.
- `token_length_chars` counts characters of the rendered card, not model tokens;
  it is a rough size proxy for equal-context ablations, not a token budget.
- A card still carries full-fidelity prose. An *ultra-compact* routing variant
  (student-adapted, minimal tokens) is an adaptation step, not part of this
  lossless restructure.
