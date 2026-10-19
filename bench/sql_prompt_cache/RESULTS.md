# Prompt cache reuse of tool_text_to_sql's internal request

`python bench/sql_prompt_cache/measure.py` — 10 fixed questions, fixed synthetic
schemas, request built by `_state_generate_sql` (model call stubbed), tiktoken
o200k. "Cacheable" = prefix shared with the previous question's request, under
the OpenAI/Azure rule (≥ 1,024 tokens, 128-token blocks). Questions 2-10.

| Schema | Before (relevance order) | After (name order) |
|---|---|---|
| 3 tables, 762 chars | 0 / 2,298 (0 %) | 0 / 2,298 (0 %) — under the 1,024-token floor |
| 25 tables, 6,113 chars (fits the 16,000-char budget) | 1,280 / 11,721 (11 %) | 11,520 / 11,721 (98 %) |
| 80 tables, 16,278 chars (budget hit, 62-63 tables kept) | 3,328 / 30,871 (11 %) | 28,672 / 30,871 (93 %) |

Same tokens sent and same tables selected for every question, before and after.
Upper bound: the provider must also route both requests to the same cache; the
real hit rate shows in `llm_usage.cached_tokens`.
