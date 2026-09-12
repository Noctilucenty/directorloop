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

Pending: filled in from the actual runs, with experiment ids and Weave links.
