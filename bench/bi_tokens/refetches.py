"""Count re-fetches in an ADK session trace with message content captured."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _call_key(call: dict) -> tuple[str, str]:
    return call["name"], json.dumps(call.get("args") or {}, sort_keys=True)


def _latest_results(contents: list[dict]) -> dict[tuple[str, str], bool]:
    """Map each tool and argument pair to its latest result's compaction status."""
    calls: dict[str, tuple[str, str]] = {}
    results: dict[tuple[str, str], bool] = {}
    for content in contents:
        for part in content.get("parts") or []:
            if (call := part.get("function_call")) and call.get("id"):
                calls[call["id"]] = _call_key(call)
            if (result := part.get("function_response")) and result.get("id") in calls:
                key = calls[result["id"]]
                results[key] = "_compacted" in (result.get("response") or {})
    return results


def count_refetches(trace: list[dict]) -> int:
    """Count new tool calls whose latest matching result was compacted in the prompt."""
    model_spans = [span for span in trace if span["name"] == "call_llm"]
    if not model_spans:
        raise ValueError("Trace has no ADK call_llm spans.")

    total = 0
    for span in model_spans:
        attrs = span["attributes"]
        request = json.loads(attrs.get("gcp.vertex.agent.llm_request", "{}"))
        response = json.loads(attrs.get("gcp.vertex.agent.llm_response", "{}"))
        if "contents" not in request or not response:
            raise ValueError("The ADK call_llm spans must include message content.")

        results = _latest_results(request["contents"])
        for part in (response.get("content") or {}).get("parts") or []:
            if (call := part.get("function_call")) and results.get(_call_key(call)):
                total += 1
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="ADK session trace JSON file")
    args = parser.parse_args()
    try:
        trace = json.loads(args.trace.read_text(encoding="utf-8"))
        count = count_refetches(trace)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"refetches": count}))


if __name__ == "__main__":
    main()
