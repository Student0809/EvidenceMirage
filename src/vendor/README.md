# Narrow compatibility layer

These primitives come from the existing PIMiner working tree, not from a fresh Git checkout. `data/assets_manifest.json` records the original file hashes and extracted symbols.

Default `attacker.mode: original` uses:

- `router.py`: original strategy discovery, summary extraction, router role/body, choice parser, full candidate-document assembly, and attempt parser. Function bodies are unchanged; only module imports and project-root binding are local.
- `prompts.py`: original black-box full-history Attacker prompt builders, unchanged.
- `attacker_role.txt`: verbatim role and attack-class instructions from `.claude/commands/step.md`, before its shell Procedure section. The Runner replaces shell scheduling; the model receives this text as its system prompt.
- `final_output.py`: original final-assistant text/tool-call projection. Intermediate messages and tool results are excluded.

Source snapshots (including the full step/route command documents) are preserved under `data/original/piminer_code/`. Tests compare extracted functions and constructed prompt text to these snapshots. The original shell session and tool permissions are not recreated. Strategy Markdown remains byte-identical to the migrated library; the Router selects from the whole library, and the Attacker receives each chosen file in full.

The optional `attacker.mode: grounded` extends the V1 refinement primitives below:

- `attacker_observer.py`: resource grants and native event capture; retained intact.
- `revision_planner.py`: public input projection, plan validation and planner prompts. `grounded_history_v1` adds the selected strategy document, complete authorized branch memory, program-owned fact IDs and structured hypothesis tests. The main loop only uses replay of fixed queries, with zero active-query budget; one malformed no-query plan can receive a bounded correction.
- `revision_checker.py`: static checks and advisory semantic review. Current grounded revisions invoke the review for each statically valid candidate, with at most one shared candidate-repair opportunity. Unknown/missing review coverage is recorded and is never an official failure or Gold judgment. Historical V1 runs did not invoke this review.
- `refinement.py`: extracted `ClaudeDecision`, `parse_payload`, `generation_prompt`; grounded generation now includes the complete projected branch memory. The old disk-state refinement controller is not imported.

`grounded_history_plan_v2` adds candidate-bound plan summaries and explicit reviews of their observed outcomes. Its memory retains the additive `grounded_history_v1` archive schema, while generation records use the new protocol name for exact replay. Plans, preservation choices and prediction assessments remain model annotations. `plan_references.py` canonicalizes only exact aliases of existing facts and records each transformation before strict validation. A plan-level advisory conflict returns to the planner with the same frozen evidence, at most two correction calls and one replacement candidate; candidate-only conflicts still use the shared one-repair opportunity. The normal path costs three calls and the complete bound is eight. Failed advisory replanning retains the statically valid original candidate and records the unresolved review.

No original project directory is added to `sys.path` by runtime code. All arms use these same primitives and model settings. Static `REVIEW` is not a semantic correctness guarantee or an official Gold judgment.
