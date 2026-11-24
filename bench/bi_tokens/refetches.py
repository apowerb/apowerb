"""Count re-fetches in an ADK session trace with message content captured."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _legacy_messages(contents: list[dict]) -> list[dict]:
    """Translate legacy ADK function parts to the schema v2 message format."""
    messages = []
    for content in contents:
        parts = []
        for part in content.get("parts") or []:
            if call := part.get("function_call"):
                parts.append(
                    {**call, "type": "tool_call", "arguments": call.get("args")}
                )
            elif result := part.get("function_response"):
                parts.append({**result, "type": "tool_call_response"})
        messages.append({"parts": parts})
    return messages


def _messages(span: dict) -> tuple[list[dict], list[dict]]:
    attrs = span["attributes"]
    if span["name"] == "call_llm":
        request = json.loads(attrs.get("gcp.vertex.agent.llm_request", "{}"))
        response = json.loads(attrs.get("gcp.vertex.agent.llm_response", "{}"))
        if "contents" in request and response:
            return _legacy_messages(request["contents"]), _legacy_messages(
                [response.get("content") or {}]
            )
    else:
        # The Dev Server restores structured messages on the completion log.
        # With span content capture enabled, the same fields are JSON strings.
        sources = [
            log["attributes"]
            for log in span.get("logs", [])
            if log["event_name"] == "gen_ai.client.inference.operation.details"
        ]
        for source in [*sources, attrs]:
            if "gen_ai.input.messages" in source and "gen_ai.output.messages" in source:
                inputs = source["gen_ai.input.messages"]
                outputs = source["gen_ai.output.messages"]
                return (
                    json.loads(inputs) if isinstance(inputs, str) else inputs,
                    json.loads(outputs) if isinstance(outputs, str) else outputs,
                )
    raise ValueError(
        "The ADK inference spans must include captured input and output message content."
    )


def _call_key(call: dict) -> tuple[str, str]:
    return call["name"], json.dumps(call.get("arguments") or {}, sort_keys=True)


def _latest_results(messages: list[dict], legacy: bool) -> dict[tuple[str, str], bool]:
    calls: dict[str, tuple[str, str]] = {}
    anonymous: dict[str, list[tuple[str, str]]] = defaultdict(list)
    results: dict[tuple[str, str], bool] = {}
    for message in messages:
        for part in message.get("parts") or []:
            call_id = part.get("id") or ""
            prefix, _, index = call_id.rpartition("_")
            # V2 synthesizes name_index IDs separately for each message. The
            # index can change between a call and its result (e.g. a text part).
            positional_id = (
                not legacy and index.isdigit() and prefix == part.get("name", prefix)
            )
            if part["type"] == "tool_call":
                key = _call_key(part)
                if call_id and not positional_id:
                    calls[call_id] = key
                else:
                    anonymous[key[0]].append(key)
            elif part["type"] == "tool_call_response":
                if call_id in calls:
                    key = calls.pop(call_id)
                else:
                    name = prefix if positional_id else part.get("name")
                    candidates = anonymous.get(name or "", [])
                    if (call_id and not positional_id) or len(set(candidates)) != 1:
                        raise ValueError(
                            "Cannot uniquely match a tool result to its call in the captured history."
                        )
                    key = candidates.pop()
                results[key] = "_compacted" in (part.get("response") or {})
    return results


def count_refetches(trace: list[dict]) -> int:
    """Count new tool calls whose latest matching result was compacted in the prompt."""
    legacy_ids = {
        span["span_id"]
        for span in trace
        if span["name"] == "call_llm" and "span_id" in span
    }
    total = 0
    found_inference = False
    for span in trace:
        legacy = span["name"] == "call_llm"
        if not legacy and (
            span["name"].split(" ", 1)[0] != "generate_content"
            or span.get("parent_span_id") in legacy_ids
        ):
            continue
        found_inference = True
        inputs, outputs = _messages(span)
        new_calls = [
            p
            for message in outputs
            for p in message.get("parts", [])
            if p["type"] == "tool_call"
        ]
        if new_calls:
            results = _latest_results(inputs, legacy)
            total += sum(results.get(_call_key(call), False) for call in new_calls)
    if not found_inference:
        raise ValueError("Trace has no ADK inference spans.")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="ADK session trace JSON file")
    args = parser.parse_args()
    try:
        trace = json.loads(args.trace.read_text(encoding="utf-8-sig"))
        count = count_refetches(trace)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"refetches": count}))


if __name__ == "__main__":
    main()
