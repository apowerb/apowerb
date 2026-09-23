# Next-node suggestions: rules alone vs rules + model

Decides whether the model-backed suggestions (`POST /api/workflows/defs/suggest-next`)
are worth showing in the Workflow Studio. **Go** when, with the model, the expected
node type lands in the first three chips at least **15 points** more often than with
the editor's rules alone, **and** the p95 latency stays **under 3 s**.

## What it measures

- `workflows.json` holds complete workflows and the agents they use. Each one is
  replayed breadth first, routes in declared order; every node added is one case:
  the partial graph so far, the node with a free slot, and the node the complete
  workflow puts there. The expected answer is fixed before any model call.
- **Rules**: a copy of `suggestNextNodes` / `mergeAiSuggestions` from
  `apowerb-ui` `src/lib/nextNodeSuggestions.js`. `rules_parity.mjs` runs the real
  JS on every case; run it whenever the editor's rules change.
- **Model**: the real route, in process. Only the quota guard, the agent list
  (taken from `workflows.json`) and the `llm_usage` write are stubbed, since there
  is no database. Latency is measured around the HTTP call, so it includes the
  route's own work; the prod timeout (`WORKFLOW_SUGGEST_TIMEOUT_S`) applies, and a
  timeout counts as a miss with its full latency.
- Rules + model = the chips the editor shows: the model's first, then the rule
  chips of other types; the first three are scored.

## Run

```bash
export ENCRYPT_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export DEFAULT_LLM_MODEL=gemini/gemini-2.5-flash DEFAULT_LLM_API_KEY=...   # the model prod would use
PYTHONPATH=src python bench/workflow_suggest/bench.py --repeats 3 --usd-per-eur 1.17 --fx-date 2026-09-23
```

`--usd-per-eur` is the ECB reference rate (dollars for one euro) used to report the
cost per call in euros. The report lands in `results/<date>-<model>.md` with the raw
calls next to it in JSON (structure and answers only, no key).

Rules parity, against an `apowerb-ui` checkout:

```bash
PYTHONPATH=src python bench/workflow_suggest/bench.py --dump-cases /tmp/cases.json
node bench/workflow_suggest/rules_parity.mjs ../apowerb-ui/src/lib /tmp/cases.json > /tmp/js.json
PYTHONPATH=src python bench/workflow_suggest/bench.py --check-parity /tmp/js.json
```

`tests/test_workflow_suggest_bench.py` checks the cases (every workflow valid, every
expected node reachable by the route) and the scoring, without calling a model.

## Limits

- The workflows were written for this bench (one comes from the docs example): they
  describe common automations, not measured usage. Adding real workflows is the best
  way to make the verdict sturdier.
- `http` is never proposed by the model (it would invent URLs), so those cases can
  only be won by the rules.
