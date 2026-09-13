# DirectorLoop workflow observability

Verified September 12, 2026. Implemented in the shared working tree; the local app is running at http://127.0.0.1:8787.

## What a judge sees

Open the [verified A/B-to-C session](http://127.0.0.1:8787/compare/abc_1a097b6ace8_52d2739b).
The connected overview shows the actual session outcome, attempts, rendered candidates and accepted candidates.
Completed branches start collapsed; a running branch opens as real stage events arrive. Click a stage for its evidence
and its corresponding Weave call. “Inspect rendered C” reveals the actual render within its attempt.

The product presents the workflow. Weave supplies the nested execution evidence, sampled model inputs, outputs,
latency, tokens and provider cost. These are model judgments and mechanical checks; they are not measured viewer retention.

## Before and after

The previous traces already had real parentage, but repeated internal operations dominated the visible tree:

```text
run_video_budget3
  audit_video_v0
    cold_review_0.0-2.0s
      model_call.vision
        openai.chat.completions.create
    cold_review_window
    diagnose_audit
    closeup_verify
  analyze_creative
  map_finding_to_repair
```

The verified new A/B-to-C session has this semantic hierarchy. Branches appear only when that work actually runs:

```text
DirectorLoop A/B to C | smoot-action-first-v1 + smoot-cinematic-reedit-v1
  Ingest and validate A and B
  Prepare matching evaluation inputs
    Prepare input A / B
    Prepare matched evaluation copy A / B
  Iteration 0 - Independent A and B audits
    Cold-audience audit A / B
      Ingest and inspect video
      Cold audience judge
        Cold audience review [actual video interval]
          existing review operation
            model_call.vision
              openai.chat.completions.create
      Diagnose weaknesses
  Align corresponding story beats
  Compare A and B in both orders
  Iteration 1 - Directed C attempt
    Direct the smallest supported C
    Render candidate C
    Verify C implements the plan
    Fresh independent audit of C
    Compare C against A and B
    Accept C or keep the inputs
    Record the A/B/C experiment lesson
  Final selection and stop reason
```

Single-video repair sessions receive equivalent original-review, attempt, render, verification, rejudge,
comparison, decision and lesson boundaries. A declined edit does not acquire fictional render/review/compare steps.
The matched A/B evaluation copies are explicitly distinct from a generated C.

## Implementation and integration

- `directorloop/observability/workflow.py`: typed stage journal, real Weave spans, recorded links, start/end events,
  context isolation, safe stack restoration and persistence after context exit, including cancellation and exceptions.
- `directorloop/runtime/director.py` and `runtime/abc.py`: semantic boundaries around existing execution. Root operation
  names remain `directorloop.run` and `directorloop.abc_run`, preserving saved-view filters.
- `directorloop/audit/review.py`: actual ingestion, prefix-review windows, diagnosis and attempted closeups.
  Existing provider spans remain untouched. Existing parallel audit workers preserve their branch context.
- DirectorRun, ABCRun and AuditReport expose optional `workflow_stages`, defaulting to an empty list for older records.
- `apps/web/src/components/WorkflowGraph.tsx`, its CSS, and `src/lib/workflow.ts`: compact overview, independent A/B
  branches, evidence drilldowns, explicit rejection/acceptance, genuine stage links and measured durations.
- `RunPage.tsx` and `ComparePage.tsx` mount the graph above their existing stepper. They receive persisted stages and
  existing job events. `WORKFLOW_STAGE` events update the graph while API jobs run.

The local journal is authoritative for the product display. Nested audits save only their own stage subtree. Terminal
events follow stage closure and persistence. A completed operation is distinct from an accepted edit. Cancellation,
failed execution and incomplete evidence remain distinct states. A failed tracing upload does not change evaluation.

Older runs use an explicitly labeled reconstruction from saved records, retaining their annotations and unknown timings.
The product sorts review-window siblings by video timestamp for reading; recorded execution timestamps remain intact.
It does not create individual trace links for historical steps that never recorded one.

## Live validation

Run: `abc_1a097b6ace8_52d2739b`.
Root call: `01a097b6-ace3-72b0-9b67-b6b92bfd89e0`.
Remote trace ID: `01a097b6-acd6-7b5e-be17-4131dc6a4ef2` (different from the root call ID).

- One C attempt; budget 60 model calls / 300 seconds. Existing reviewer and selector models preserved.
- Actual use: 39 model calls, 93.183 seconds, 265,468 tokens.
- C shortened A's 1.11-second ending pause. It rendered, passed mechanical verification, received a fresh audit,
  and was compared against A and B in both presentation orders.
- Decision: reject C and keep A. No reliable gain on target pacing/payoff; overall versus A was unstable.
- Weave displayed $0.5618. Runtime `cost_usd` remains null because no verified local price table is configured.
- Read-only SDK verification: 228 calls, one root, zero orphan parents, all 60 local workflow stages uploaded and finished,
  all 60 semantic parent relationships correct. Eleven stages have preserved audit wrappers between semantic parents.
- No mocked stages. This run confirms execution and observability, not audience performance or universal video quality gains.

Validation: all 101 backend tests passed; Ruff and `git diff --check` passed. Frontend production build and 11 workflow
contract checks passed. Chrome confirmed the compact overview, render and decision drilldowns, real links and historical
annotation preservation. The paid validation was launched through the CLI; the browser inspected its saved result.
Live event handling is covered by runtime/API and frontend contract checks, not a second paid browser-launched run.

## Saved Weave views and demonstration

- [Demo - A/B to C](https://wandb.ai/leondragon3798-curio/directorloop/weave/traces?view=traces_2026-09-12_21-46-49-929)
- [Demo - Repair loops](https://wandb.ai/leondragon3798-curio/directorloop/weave/traces?view=traces_2026-09-12_21-50-42-699)
- [Full verified trace](https://wandb.ai/leondragon3798-curio/directorloop/weave/calls/01a097b6-ace3-72b0-9b67-b6b92bfd89e0)

Saved views show root sessions and their decision, status, tokens, cost and latency. The new raw workflow journal column
was hidden from the A/B-to-C view and the existing view saved. Failed runs remain visible.

Demo sequence: show the product overview, inspect C, inspect its decision, then open that stage in Weave.
Explain: “It tried a supported edit, verified the render, reviewed the result independently, and kept the original when
the evidence did not establish a gain.” Expand or clear the semantic tree filter to expose every real provider call.
The remaining low-level audit/provider wrappers are intentional evidence, not unfinished cleanup.

The Weave project is private. Present from the signed-in Chrome session; the URLs alone do not grant judge access.

## Evidence files and coordination

The operator retains the original dashboard evidence separately; private receipts and screenshots are not included in this source release.
Receipts: `hierarchy-live-run.json`, `hierarchy-remote-verification.json`.
Screenshots: `connected-workflow.png`, `workflow-decision.png`, `semantic-weave-hierarchy.png`.
The original operator handoff is a private coordination artifact.

Claude's broader frontend redesign remains in the same uncommitted working tree. This work adds instrumentation and
the graph integration; it does not replace the redesign. Do not reset, blanket-stage, or claim all working-tree changes
as this task. Prompts, evaluator logic, models, retry behavior, repair choices and acceptance thresholds were preserved.
