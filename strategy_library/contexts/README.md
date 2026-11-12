# Strategy example contexts

These files replace 34 unavailable historical-run context references with 17 full contexts from the current local datasets. They are reference material, not newly verified attack results.

Each JSON contains the full `context_with_placeholder`, its SHA-256, the source dataset path and SHA-256, source line numbers, task identifiers, injection vectors, and the matching rationale. Paths are relative to the repository root. `manifest.json` maps each strategy example and original historical reference to its replacement.

**Historical identity is unverified for every replacement.** The old run files are unavailable. Unique task matching does not prove identical historical bytes. Existing descriptions and excerpts in the strategy documents are retained as historical evidence, and may differ from current data (including listing length, surrounding code, or injection position). Do not interpret these replacements as replay validation or new successful attacks.

For AgentDojo user_task_35, select the multi-file listing at step 3 rather than the single recipe at step 5. For user_task_13, select the feedback injection vector at step 9. For user_task_36, steps 3 and 5 contain identical context; both source rows are recorded without claiming a historical step match.

To establish historical identity, recover the original run JSON and compare its context byte-for-byte. Preserve discrepancies and provenance when replacing current-data reconstructions with historical artifacts.
