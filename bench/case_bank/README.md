# Case bank: does re-reading past runs help an agent succeed?

Decides whether `case_bank_enabled` is worth turning on. **Go** when, with K=4
past cases, the success rate gains **at least 10 points** over K=0, the tool
error rate does not rise, and input tokens per task grow by **at most 10 %**.
Thresholds were set before the first run.

## What it measures

- A synthetic invoice-intake agent (fictitious suppliers, seeded amounts and
  dates). Its instruction is short on purpose; two house rules are missing from
  it and can only be learned from experience:
  - `tool_save_invoice` wants the supplier id from `tool_lookup_supplier`
    (a name is refused with `success: false`);
  - an invoice above 10 000 must go through `tool_flag_for_review` first.
    Nothing says so: the run "succeeds", only the final check counts it wrong.
- The expected answer of every task is fixed in `build_tasks()` before any
  model call (`--dump-tasks` prints them).
- **Strict split.** The bank is filled on tasks A (suppliers 1-6, the agent
  works with K=0 and each checked outcome becomes a case); evaluation runs on
  tasks B (suppliers 7-12), which are never written to the bank.
- Arms on B: `K0` (empty bank), `K4` (4 cases, at most 2 failures),
  `K4_pos_only` (4 cases, successes only).
- Real code: the ADK agent and Runner, `make_case_bank_before_model_callback`
  and `CaseBank` (SQLite). Input tokens are read from `usage_metadata` when the
  model reports them; otherwise `chars/4` over the request, tools included
  (`tokens_source` says which).
- `status_label_agrees_with_check`: how often the signal production has
  (`agent_runs.status`: the run did not raise) agrees with the real check.

## Run

```bash
PYTHONPATH=src python bench/case_bank/bench.py --model scripted          # plumbing only
export GEMINI_API_KEY=...                                                 # the model prod would use
PYTHONPATH=src python bench/case_bank/bench.py --model gemini/gemini-2.5-flash --repeats 3 --save <date>-gemini-2.5-flash
```

`--model scripted` replaces the model with a script that picks each rule up
half the time on its own and always once a successful case shows the tool. It
proves the bank changes what the model sees and does; **it never measures a
gain**. Only a real model does.

## Limits

- One synthetic agent, two rules: a gain here says the mechanism can work, not
  how much it brings to a given customer agent.
- The cases are labelled by the final check, as Memento labels them against a
  ground truth. In production the label is the run status, which misses silent
  failures (the threshold rule): see `status_label_agrees_with_check`.
