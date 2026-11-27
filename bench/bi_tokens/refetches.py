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


def _positional_tool(call_id: str) -> str | None:
    name, _, index = call_id.rpartition("_")
    if (
        name
        and index.isascii()
        and index.isdecimal()
        and (index == "0" or not index.startswith("0"))
    ):
        return name
    return None


def _latest_results(
    messages: list[dict], legacy: bool
) -> tuple[dict[tuple[str, str], bool], int]:
    calls: dict[str, tuple[str, str]] = {}
    anonymous: dict[str, list[tuple[str, str]]] = defaultdict(list)
    results: dict[tuple[str, str], bool] = {}
    unmatched = 0
    seen_result = False
    for message in messages:
        for part in message.get("parts") or []:
            call_id = part.get("id") or ""
            positional_tool = None if legacy else _positional_tool(call_id)
            if part["type"] == "tool_call":
                if seen_result:
                    # Name-only matching is limited to the current batch of calls.
                    anonymous.clear()
                    seen_result = False
                key = _call_key(part)
                # ADK v2 fills missing IDs with a per-message tool_name_index.
                # A provider ID with that shape has the same ambiguity.
                if call_id and positional_tool != key[0]:
                    calls[call_id] = key
                else:
                    anonymous[key[0]].append(key)
            elif part["type"] == "tool_call_response":
                seen_result = True
                name = positional_tool if call_id else part.get("name")
                name_matches = anonymous.get(name or "", [])
                exact_match = calls.get(call_id)
                candidates = set(name_matches)
                if exact_match is not None:
                    candidates.add(exact_match)
                if len(candidates) != 1:
                    unmatched += 1
                    # An unresolved newer result may supersede an older match.
                    for key in candidates:
                        results.pop(key, None)
                    continue
                if exact_match is not None:
                    key = calls.pop(call_id)
                else:
                    key = name_matches.pop()
                results[key] = "_compacted" in (part.get("response") or {})
    return results, unmatched


def count_refetches(trace: list[dict]) -> dict[str, int]:
    """Count confirmed re-fetches and unmatched historical results in scored prompts."""
    legacy_ids = {
        span["span_id"]
        for span in trace
        if span["name"] == "call_llm" and "span_id" in span
    }
    total = 0
    unmatched = 0
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
            results, unmatched_in_prompt = _latest_results(inputs, legacy)
            unmatched += unmatched_in_prompt
            total += sum(results.get(_call_key(call), False) for call in new_calls)
    if not found_inference:
        raise ValueError("Trace has no ADK inference spans.")
    return {"refetches": total, "unmatched": unmatched}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="ADK session trace JSON file")
    args = parser.parse_args()
    try:
        trace = json.loads(args.trace.read_text(encoding="utf-8-sig"))
        report = count_refetches(trace)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
