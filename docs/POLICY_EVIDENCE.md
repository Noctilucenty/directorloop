# Policy evidence and transfer protocol

## Transfer protocol (written before any Video B result existed, 2026-09-12 13:20 PDT)

Question: does experiment evidence from Video A change which experiment the agent tries FIRST on an unseen Video B,
and is that first choice better on B?

1. The creative policy starts empty (version 0). With no experiments, learned mode and no-memory mode rank
   candidates identically (unit test `test_memory_changes_the_first_experiment_and_no_memory_mode_does_not`).
2. Video A: `AP-TIPPE-V4` (Curio short, 13.9 s). Run one experiment: control plus the three designer-selected arms,
   3 probe trials, pairwise preference with both orders. Every arm outcome (win, loss, neutral, rejected) is recorded.
   One observation from A was seen before this protocol was written: during the fitness smoke test, moving the payoff
   earlier won the opening comparison but lost the full-video comparison (the model said it spoils the reveal).
3. Video B: `AP-LAZE-NARRATED-01` (Curio short, 15.6 s). Its genome and candidate list were computed before A ran,
   but no experiment or evaluation of B's variants has run. B was chosen because it was the second video already
   analyzed, not because of any B result.
4. On B, compute the candidate ranking in no-memory mode and in learned mode (policy after A). Save both rankings
   before evaluating anything on B.
5. Evaluate EVERY valid candidate mutation on B once, with policy recording off, to learn which arms actually win on B
   under the same frozen suite and decision rule.
6. Report for each mode: the first mutation tried, its outcome on B, the number of attempts until the first win in
   that ranking order (or "no win among N candidates"), and the model calls and evaluation time spent until then.
7. If learned mode does not choose a better first experiment, report that. A single A/B pair is pilot evidence.

## Results

All evidence below is MODEL JUDGMENT (gpt-5.6-terra viewer probes on sampled frames plus the whisper transcript of each
rendered file, pairwise preference in both presentation orders). None of it is audience data.

### Video A: AP-TIPPE-V4 (policy v0 to v3)

| Experiment | Status | Arms and outcome | Time | Calls | Trace |
|---|---|---|---|---|---|
| `cexp_1a097415c48_7bf9dc2b` | completed | PATTERN_INTERRUPT neutral; PAYOFF_EARLIER loss (won the opening comparison, lost the whole video: it spoils the reveal); CONTEXT_COMPRESSION neutral. Decision: no clear winner | 14.6 s | 68 | [call](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09741-5c48-7404-9c6c-09d72832d0a9) |
| `cexp_1a0973ea047_79e85d42` | superseded, kept | Same protocol, same per-arm outcome classes. Superseded because a tracing defect detached child calls from the trace; the policy was reset and the run repeated | 25.3 s | 68 | [call](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a0973e-a047-7778-b57f-c0d31aa48479) |

### Video B transfer: AP-LAZE-NARRATED-01 (rankings saved before any B evaluation)

| Mode | First experiment chosen | Its outcome on B | Attempts until first win |
|---|---|---|---|
| No memory | PAYOFF_EARLIER | loss | no win among 4 candidates |
| Learned (policy v3 from Video A) | RESULT_FIRST | loss | no win among 4 candidates |

Every valid candidate was evaluated on B once with policy recording off (`cexp_1a09744b816_2009893a`, 51.1 s, 80 calls,
[call](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09744-b816-7707-a3ca-176e3cd99df1)): RESULT_FIRST loss,
PATTERN_INTERRUPT neutral, PAYOFF_EARLIER loss, CONTEXT_COMPRESSION loss.

Result: memory from Video A changed the first experiment on Video B, but the learned choice was not better. Both first
choices lost, and no candidate won on B. This is a losing result and is kept as such: one A/B pair is pilot evidence,
and it does not show that the learned policy transfers. The transfer report is
`data/creative/transfer/20260912-131738_aplaze_transfer_report.json` (local), with the rankings saved before evaluation in
`20260912-131738_aplaze_rankings_before_evaluation.json`.

After the transfer comparison was saved, B's outcomes were recorded into the policy (v3 to v7): PAYOFF_EARLIER REJECTED
(two losses, no wins), RESULT_FIRST CONTRADICTED, CONTEXT_COMPRESSION CONTRADICTED, PATTERN_INTERRUPT PROPOSED (neutral
only). A copy of the v7 store taken before the runtime loop wrote any evidence is kept locally as
`data/creative/policy_v7_before_runtime_loop.json`.
