"""Agent evaluation (POC).

Scaffolding for the AgentOps "evaluation" pillar Tracing (th2pulse) and
Monitoring (Usage/Logging) already have. Off by default
(`EVALUATION_ENABLED=false`) and never wired into the agent request path:
today this package is a library plus an offline runner
(`poc_runner.py`), not a shipped feature.

Two evaluator families live under ``evaluators/``:

- deterministic (``tool_execution_outcome.py``): no LLM call, reads real
  data already captured by th2pulse / ADK.
- LLM-judge (``task_completion_judge.py``): calls a judge model that MUST
  differ from the model being judged (self-preference bias).
"""
