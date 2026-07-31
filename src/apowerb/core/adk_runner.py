import aiohttp
import asyncio
import codecs
import os
import re
from typing import Dict, Any, AsyncGenerator, List, Optional
from datetime import datetime, timezone
import json
from logging import getLogger

from apowerb.configs.settings import get_settings

logger = getLogger(__name__)

settings = get_settings()


# How many full re-runs of the agent we attempt before giving up on a
# rate-limited request. The interactive chat path can't afford the long,
# open-ended retry budget the backlog worker uses, so we cap at 2.
_CHAT_MAX_RATE_LIMIT_RETRIES = 2

# Floor for the wait between retries — Gemini's RetryInfo can come back
# as 0 or absent during transient bursts, in which case we still wait long
# enough for the per-minute quota window to roll forward.
_CHAT_RATE_LIMIT_DEFAULT_DELAY = 30.0

# Cap on how long we'll keep the user waiting on a single retry. Above
# this, we surface the error and let the UI tell the user to try again.
_CHAT_RATE_LIMIT_MAX_DELAY = 60.0


def _chunk_signals_rate_limit(chunk: str) -> bool:
    """True if a forwarded SSE chunk encodes a Gemini 429 / RateLimitError.

    The ADK web server serialises tool / model errors into the SSE stream
    as ``data: {"error": "litellm.RateLimitError: ... 429 ..."}``. This
    function pattern-matches that envelope so we can intercept and retry
    instead of forwarding the error to the user.
    """
    if not chunk:
        return False
    return (
        "RateLimitError" in chunk
        or "RESOURCE_EXHAUSTED" in chunk
        or ('"code": 429' in chunk)
        or ("429 Too Many Requests" in chunk)
    )


def _parse_retry_delay_from_chunk(chunk: str) -> Optional[float]:
    """Pull the retry delay (seconds) out of a 429 SSE chunk.

    Mirrors ``backlog_worker._parse_retry_delay_from_error`` but reads
    the SSE payload string directly. Gemini emits both a structured
    ``"retryDelay": "22s"`` and a human-readable ``Please retry in
    22.6s``; either suffices. The nested-JSON escapes (``\\"``) in the
    SSE payload are normalised first so a single regex handles every
    nesting depth.
    """
    if not chunk:
        return None
    normalised = chunk.replace('\\"', '"')
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', normalised)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", normalised)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# --- Garde anti-reponse-vide (chat) ---------------------------------------
# Gemini renvoie parfois un tour assistant VIDE ('' ) : panne de generation
# (artefacts de thinking), non corrigeable par prompt. On detecte un stream sans
# AUCUN contenu visible (ni texte, ni function_call, ni erreur) et on emet un repli
# -> l'utilisateur n'est jamais laisse devant un tour blanc sans directive.
_EMPTY_FALLBACK_TEXT = (
    "Desole, je n'ai pas reussi a formuler une reponse complete a l'instant. "
    "Peux-tu reformuler ta demande, ou me dire ou tu veux qu'on reprenne ?"
)
# Au-dela de ce volume, un tour a forcement produit du contenu (un tour vide est
# minuscule) : on arrete de bufferiser -> pas de double-retention sur gros tool result.
_GUARD_BUFFER_CAP = 65536  # 64 KiB


def _empty_fallback_enabled() -> bool:
    """Lu DYNAMIQUEMENT a chaque tour. CHAT_EMPTY_FALLBACK=0 desactive la garde ;
    pris en compte au PROCHAIN tour SANS attendre (pas une constante d'import).
    Un changement de .env reste soumis au restart du process ; ce flag couvre le
    cas d'un toggle pose via l'environnement deja charge. Defaut: actif."""
    return os.getenv("CHAT_EMPTY_FALLBACK", "1") != "0"


def _chat_empty_max_retries() -> int:
    """Nombre de RE-TIRAGES silencieux d'un tour de chat VIDE avant le repli.

    Un tour Gemini vide est TRANSITOIRE (artefacts de thinking) et, par definition
    de ``_stream_is_empty``, ne porte AUCUN appel d'outil -> re-emettre la requete
    n'a aucun effet de bord EXTERNE (pas de double INSERT/appel d'API ; meme logique
    que le re-tirage des drafts #225). Note : ADK ``_append_new_message_to_session``
    est inconditionnel -> le re-POST ajoute une 2e fois le message user dans
    l'historique de session (comme le retry rate-limit, en prod depuis le 07/05).
    Benin (tours vides rares, aucun outil rejoue), mais a surveiller sur sessions
    longues. Lu DYNAMIQUEMENT. ``CHAT_EMPTY_MAX_RETRIES=0`` -> comportement historique
    (repli direct, aucun re-tirage). Defaut: 1 (re-generer une fois avant d'abandonner)."""
    try:
        return max(0, int(os.getenv("CHAT_EMPTY_MAX_RETRIES", "1")))
    except ValueError:
        return 1


def _stream_is_empty(buffered: str) -> bool:
    """True si le flux SSE forwarde n'a porte AUCUN contenu assistant visible
    (aucun texte non vide, aucun function_call/response, aucune donnee inline/code)
    et AUCUNE erreur. Conservateur : au moindre signe de contenu/erreur -> False."""
    if not buffered.strip():
        return True
    _CONTENT_PART_KEYS = (
        "functionCall", "function_call", "functionResponse", "function_response",
        "inlineData", "inline_data", "fileData", "file_data",
        "executableCode", "executable_code", "codeExecutionResult", "code_execution_result",
    )
    for line in buffered.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            if data_str.strip():
                return False
            continue
        if not isinstance(data, dict):
            return False
        if (data.get("error") or data.get("errorCode") or data.get("error_code")
                or data.get("errorMessage") or data.get("error_message")):
            return False
        content = data.get("content")
        if isinstance(content, dict):
            for part in content.get("parts", []) or []:
                if isinstance(part, dict):
                    if (part.get("text") or "").strip():
                        return False
                    if any(part.get(k) for k in _CONTENT_PART_KEYS):
                        return False
        elif isinstance(content, str) and content.strip():
            return False
        if any(data.get(k) for k in _CONTENT_PART_KEYS):
            return False
        delta = data.get("delta")
        if isinstance(delta, dict) and (delta.get("content") or "").strip():
            return False
        if (data.get("text") or "").strip():
            return False
    return True


def _empty_fallback_event() -> str:
    """Evenement SSE de repli, meme forme que les events ADK (content.parts[].text)."""
    return "data: " + json.dumps(
        {"content": {"role": "model", "parts": [{"text": _EMPTY_FALLBACK_TEXT}]}}
    ) + "\n\n"


async def _handle_possibly_empty_turn(
    *,
    agent_name: str,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    first_attempt_buffer: str,
) -> AsyncGenerator[str, None]:
    """Gere un 1er tour potentiellement VIDE : re-tire en silence avant le repli.

    Appele UNIQUEMENT quand la garde est active et que le 1er tour n'a pas
    deborde du buffer. Si le 1er tour portait du contenu -> no-op. Sinon (vide :
    ni texte, ni outil, ni erreur) on re-emet la requete jusqu'a
    ``_chat_empty_max_retries()`` fois ; des qu'un re-tirage porte du contenu on
    s'arrete. Si tous les re-tirages restent vides -> on emet le repli historique.

    Sur (le rare) cas d'un rate-limit pendant un re-tirage : on transmet le chunk
    d'erreur (c'est lui le message visible) et on s'arrete, sans repli blanc.

    Les chunks d'un 1er tour vide ont deja ete forwardes en live, mais ils ne
    portaient AUCUN contenu visible (c'est la definition de ``_stream_is_empty``) et
    le front ignore ``[DONE]`` sans couper la lecture -> re-streamer le tour suivant
    est sur (verifie sur useStreaming.js : la boucle ne s'arrete que sur EOF HTTP).
    Cote serveur, ADK ajoute une 2e fois le message user en session (append
    inconditionnel) : benin (cf _chat_empty_max_retries), pas d'outil rejoue.
    """
    try:
        if not _stream_is_empty(first_attempt_buffer):
            return  # le 1er tour portait du contenu -> rien a faire
    except Exception:  # noqa: BLE001 -- la garde ne doit JAMAIS casser un tour
        logger.warning("[STREAM] empty-guard raised for %s, skipping", agent_name, exc_info=True)
        return

    max_retries = _chat_empty_max_retries()
    for empty_attempt in range(max_retries):
        logger.warning(
            "[STREAM] empty assistant turn for %s -- re-tirage %d/%d",
            agent_name, empty_attempt + 1, max_retries,
        )
        buf: list[str] = []
        blen = 0
        overflow = False
        async for chunk in _stream_adk_agent_once(url=url, headers=headers, payload=payload):
            if _chunk_signals_rate_limit(chunk):
                # rate-limit pendant le re-tirage : l'erreur EST le message -> on la
                # transmet et on s'arrete (pas de repli blanc par-dessus une erreur).
                yield chunk
                return
            if not overflow:
                buf.append(chunk)
                blen += len(chunk)
                if blen > _GUARD_BUFFER_CAP:
                    overflow = True
                    buf = []
            yield chunk
        if overflow:
            return  # tour volumineux -> forcement non vide
        try:
            if not _stream_is_empty("".join(buf)):
                return  # un re-tirage a produit du contenu -> termine
        except Exception:  # noqa: BLE001
            return  # indecidable -> on suppose du contenu, pas de double repli

    logger.warning(
        "[STREAM] empty assistant turn for %s -- emitting fallback after %d retries",
        agent_name, max_retries,
    )
    yield _empty_fallback_event()


async def _stream_adk_agent_once(
    *,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Single shot of the ADK SSE stream. Yields raw SSE chunks.

    Factored out of ``stream_adk_agent`` so the retry wrapper can call it
    multiple times with the same payload.
    """
    timeout = aiohttp.ClientTimeout(
        total=None,  # No total timeout for streaming
        connect=30,  # 30s to establish connection
        sock_read=900,  # 15min max wait between chunks (agents may run long tool chains)
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[STREAM] ADK server error: {response.status} - {error_text}")
                    yield f"data: {json.dumps({'error': error_text, 'status': response.status})}\n\n"
                    return

                content_type = response.headers.get("Content-Type", "")
                logger.info(f"[STREAM] Response content-type: {content_type}")

                if "text/event-stream" in content_type:
                    # Forward SSE bytes as-is. Iterate raw chunks rather than
                    # lines: aiohttp's line iterator caps each line at ~64 KB
                    # and raises "Chunk too big" when ADK emits a large SSE
                    # event (e.g. a tool result with a big payload, or the
                    # final assistant text after a long tool chain). The
                    # downstream client splits on the \n\n delimiter itself,
                    # so we just need to stream the bytes through intact.
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    async for chunk in response.content.iter_any():
                        text = decoder.decode(chunk)
                        if text:
                            yield text
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        yield tail
                else:
                    # Non-streaming response - wrap in SSE format
                    data = await response.json()
                    yield f"data: {json.dumps(data)}\n\n"
                    yield "data: [DONE]\n\n"

        except aiohttp.ClientError as e:
            logger.error(f"[STREAM] Connection error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        except Exception as e:
            logger.error(f"[STREAM] Unexpected error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"


async def stream_adk_agent(
    agent_name: str,
    user_id: str,
    session_id: str,
    new_message: Dict[str, Any],
    base_url: str = None,
    streaming: bool = True,
    token: str = None,
) -> AsyncGenerator[str, None]:
    """
    Stream SSE events from ADK agent in real-time, with automatic retry on
    Gemini ``RateLimitError`` (429 RESOURCE_EXHAUSTED).

    The interactive chat path used to forward the 429 straight to the user,
    leaving the LLM to hallucinate a fake answer (e.g. claim it INSERTed
    into SuiviAR when no tool was actually called — live regression
    2026-05-07 14:56 UTC). We now intercept the rate-limit chunk, wait
    out the provider-supplied ``retryDelay`` (capped at
    ``_CHAT_RATE_LIMIT_MAX_DELAY``), and re-issue the same request up to
    ``_CHAT_MAX_RATE_LIMIT_RETRIES`` times. Mirror of the
    ``scheduler/backlog_worker.py`` retry behaviour, scoped to a single
    user-visible turn.

    Yields each SSE line as it arrives. When a retry is triggered, an
    advisory ``data: {"info":"rate_limit_retry", ...}`` event is yielded
    so the frontend can surface a transient banner instead of a hard
    error.
    """
    if base_url is None:
        base_url = settings.root_path

    url = f"{base_url}/run_sse"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "appName": agent_name,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": new_message,
        "streaming": streaming,
    }

    logger.info(f"[STREAM] Starting SSE stream to {url} for agent {agent_name}")

    for attempt in range(_CHAT_MAX_RATE_LIMIT_RETRIES + 1):
        is_last_attempt = attempt == _CHAT_MAX_RATE_LIMIT_RETRIES
        saw_rate_limit = False
        retry_delay: Optional[float] = None
        content_buffer: list[str] = []
        buffered_len = 0
        overflowed = False

        async for chunk in _stream_adk_agent_once(
            url=url, headers=headers, payload=payload,
        ):
            if _chunk_signals_rate_limit(chunk):
                saw_rate_limit = True
                if is_last_attempt:
                    # Out of retry budget — forward the original error.
                    yield chunk
                    break

                parsed_delay = _parse_retry_delay_from_chunk(chunk)
                retry_delay = parsed_delay or _CHAT_RATE_LIMIT_DEFAULT_DELAY
                if retry_delay > _CHAT_RATE_LIMIT_MAX_DELAY:
                    # Provider asked for too long a wait — better to
                    # surface the original error than block the chat.
                    logger.warning(
                        "[STREAM] rate-limit retry_delay=%.1fs exceeds cap "
                        "(%.1fs), forwarding error to user",
                        retry_delay, _CHAT_RATE_LIMIT_MAX_DELAY,
                    )
                    yield chunk
                    return

                logger.warning(
                    "[STREAM] rate-limit hit on attempt %d, retrying in %.1fs",
                    attempt + 1, retry_delay,
                )
                yield (
                    "data: "
                    + json.dumps({
                        "info": "rate_limit_retry",
                        "delay_seconds": retry_delay,
                        "attempt": attempt + 1,
                        "max_attempts": _CHAT_MAX_RATE_LIMIT_RETRIES + 1,
                    })
                    + "\n\n"
                )
                # Drain the rest of this attempt's stream silently — the
                # error chunk has been emitted by ADK and any follow-up
                # bytes belong to the failed attempt.
                break
            if not overflowed:
                content_buffer.append(chunk)
                buffered_len += len(chunk)
                if buffered_len > _GUARD_BUFFER_CAP:
                    overflowed = True
                    content_buffer = []  # un tour aussi long n'est jamais vide
            yield chunk

        if not saw_rate_limit:
            if _empty_fallback_enabled() and not overflowed:
                # 1er tour potentiellement vide : re-tirage silencieux puis repli.
                async for ev in _handle_possibly_empty_turn(
                    agent_name=agent_name, url=url, headers=headers,
                    payload=payload, first_attempt_buffer="".join(content_buffer),
                ):
                    yield ev
            return  # successful stream

        if is_last_attempt:
            # All retries exhausted; emit a final marker so the UI can
            # show a clear "rate limit persisted" message.
            yield (
                "data: "
                + json.dumps({
                    "error": (
                        "Rate limit persisted after retries — try again "
                        "in a minute."
                    ),
                })
                + "\n\n"
            )
            return

        await asyncio.sleep((retry_delay or _CHAT_RATE_LIMIT_DEFAULT_DELAY) + 1.0)


async def run_adk_agent(
    agent_name: str,
    user_id: str,
    session_id: str,
    new_message: Dict[str, Any],
    base_url: str = None,
    run_mode: str = "run",  # sse
    streaming=False,
    token: str = None,
) -> Dict[str, Any]:
    """
    Run an ADK agent by making a POST request to the ADK endpoint.
    Args:
        agent_name: Name of the agent (e.g., "my_sample_agent")
        user_id: User ID (e.g., "u_123")
        session_id: Session ID (e.g., "s_123")
        data: Dictionary containing the newMessage data
        base_url: Base url address (default: "localhost:8000")
        run_mode: "run" or "run_sse"

    Returns:
        Dictionary containing the response data

    Raises:
        aiohttp.ClientError: If the HTTP request fails
        ValueError: If the response contains an error
    """
    if base_url is None:
        base_url = settings.root_path
    url = f"{base_url}/{run_mode}"

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "appName": agent_name,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": new_message,
    }
    if run_mode == "run_sse":
        payload["streaming"] = streaming

    # Added timeout to prevent hangs on heavy Mistral runs.
    # sock_read=900 matches the streaming path — heavy tool chains can take several minutes.
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=30,
        sock_read=900,
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")

                # Handle SSE stream response
                if "text/event-stream" in content_type:
                    return await _collect_sse_response(response)

                # Handle regular JSON response
                return await response.json()
        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"Failed to run ADK agent: {e}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse response JSON: {e}")


async def _collect_sse_response(response) -> Dict[str, Any]:
    """
    Collect and parse SSE stream response, returning the final result.
    """
    collected_content = []
    final_result = None

    async for line in response.content:
        decoded_line = line.decode("utf-8").strip()

        if not decoded_line:
            continue

        if decoded_line.startswith("data: "):
            data_str = decoded_line[6:]  # Remove "data: " prefix

            if data_str == "[DONE]":
                continue

            try:
                data = json.loads(data_str)

                # Collect content from various possible formats
                if "content" in data:
                    if isinstance(data["content"], dict) and "parts" in data["content"]:
                        for part in data["content"]["parts"]:
                            if "text" in part:
                                collected_content.append(part["text"])
                    elif isinstance(data["content"], str):
                        collected_content.append(data["content"])
                elif "delta" in data and "content" in data["delta"]:
                    collected_content.append(data["delta"]["content"])
                elif "text" in data:
                    collected_content.append(data["text"])

                # Keep track of the last complete event for metadata
                final_result = data

            except json.JSONDecodeError:
                # Plain text chunk
                if data_str:
                    collected_content.append(data_str)

    # Build response with collected content
    full_content = "".join(collected_content)

    if final_result:
        # Return the last event structure with aggregated content
        if "content" in final_result:
            if isinstance(final_result["content"], dict):
                final_result["content"]["parts"] = [{"text": full_content}]
            else:
                final_result["content"] = full_content
        else:
            final_result["content"] = full_content
        return final_result

    # Fallback response structure
    return {
        "content": full_content,
        "role": "assistant",
    }


async def list_adk_sessions(
    agent_name: str,
    user_id: str,
    base_url: str = None,
    token: str = None,
) -> List[Dict[str, Any]]:
    """
    List all sessions for a user in an ADK agent.
    GET /apps/{agent}/users/{user}/sessions
    """
    if base_url is None:
        base_url = settings.root_path

    url = f"{base_url}/apps/{agent_name}/users/{user_id}/sessions"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.warning(f"Failed to list sessions for {agent_name}/{user_id}: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse sessions for {agent_name}/{user_id}: {e}")
            return []


async def get_adk_session(
    agent_name: str,
    user_id: str,
    session_id: str,
    base_url: str = None,
    token: str = None,
) -> Dict[str, Any]:
    """
    Retrieve an ADK session with its full event history.
    GET /apps/{agent}/users/{user}/sessions/{session}
    """
    if base_url is None:
        base_url = settings.root_path

    url = f"{base_url}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientResponseError:
            # Preserve the original status code so the caller can surface
            # the real HTTP semantics (e.g. 404 when the ADK session does
            # not exist yet) instead of collapsing everything into a 500.
            raise
        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"Failed to get ADK session: {e}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse response JSON: {e}")


def _truncate(text: str, max_len: int = 200) -> str:
    """Truncate text to max_len characters, appending '...' if truncated."""
    if not text or len(text) <= max_len:
        return text or ""
    return text[:max_len] + "..."


def _parse_timestamp(ts: Any) -> float | None:
    """
    Parse a timestamp that may be a float (unix seconds) or an ISO 8601 string.
    Returns unix timestamp as float, or None if unparseable.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            # Try ISO 8601 parsing
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
        try:
            return float(ts)
        except (ValueError, TypeError):
            pass
    return None


def _timestamp_to_iso(ts: Any) -> str | None:
    """Convert a timestamp (float or ISO string) to ISO 8601 string."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    if isinstance(ts, str):
        return ts
    return None


def parse_session_to_trace(session_data: dict, agent_name: str) -> dict:
    """
    Transform raw ADK session events into a structured trace for the supervision UI.

    Args:
        session_data: The raw session dict returned by get_adk_session(),
                      expected to have "events" list and "id" field.
        agent_name: The display name of the agent.

    Returns:
        A dict with session_id, agent_name, started_at, ended_at, status,
        summary, and steps list.
    """
    events = session_data.get("events", [])
    session_id = session_data.get("id", "")
    steps: List[dict] = []
    agents_involved: set = set()
    total_input_tokens = 0
    total_output_tokens = 0
    total_thoughts_tokens = 0
    total_cached_tokens = 0

    for event in events:
        author = event.get("author", "")
        timestamp = event.get("timestamp")
        content = event.get("content")

        if author:
            agents_involved.add(author)

        # Accumulate token usage from usage_metadata if present
        usage = event.get("usage_metadata") or {}
        total_input_tokens += usage.get("prompt_token_count", 0) or 0
        total_output_tokens += usage.get("candidates_token_count", 0) or 0
        total_thoughts_tokens += usage.get("thoughts_token_count", 0) or 0
        total_cached_tokens += usage.get("cached_content_token_count", 0) or 0

        # Process content parts
        if content and content.get("parts"):
            for part in content["parts"]:
                step: dict | None = None

                if part.get("thought") and part.get("text"):
                    step = {
                        "type": "thinking",
                        "content": part["text"],
                        "details": None,
                    }
                elif part.get("functionCall"):
                    fc = part["functionCall"]
                    func_name = fc.get("name", "")
                    func_args = fc.get("args", {})
                    step = {
                        "type": "tool_call",
                        "content": func_name,
                        "details": {
                            "name": func_name,
                            "args": func_args,
                        },
                    }
                elif part.get("functionResponse"):
                    fr = part["functionResponse"]
                    func_name = fr.get("name", "")
                    response_obj = fr.get("response", {})
                    response_str = json.dumps(response_obj) if isinstance(response_obj, dict) else str(response_obj)
                    step = {
                        "type": "tool_result",
                        "content": _truncate(response_str, 200),
                        "details": {
                            "name": func_name,
                            "response_preview": _truncate(response_str, 500),
                        },
                    }
                elif part.get("text"):
                    if author == "user":
                        step = {
                            "type": "user_input",
                            "content": part["text"],
                            "details": None,
                        }
                    else:
                        step = {
                            "type": "agent_response",
                            "content": part["text"],
                            "details": None,
                        }

                if step is not None:
                    step["author"] = author
                    step["timestamp"] = _timestamp_to_iso(timestamp)
                    step["duration_ms"] = None  # computed below
                    step["index"] = len(steps)
                    steps.append(step)

        # Check for agent handoff / transfer
        actions = event.get("actions") or {}
        transfer_target = actions.get("transfer_to_agent")
        if transfer_target:
            handoff_step = {
                "type": "handoff",
                "author": author,
                "timestamp": _timestamp_to_iso(timestamp),
                "duration_ms": None,
                "content": f"Transfer to {transfer_target}",
                "details": {"target_agent": transfer_target},
                "index": len(steps),
            }
            steps.append(handoff_step)
            agents_involved.add(transfer_target)

    # Compute duration_ms between consecutive steps
    for i in range(len(steps)):
        if i == 0:
            steps[i]["duration_ms"] = 0
            continue
        curr_ts = _parse_timestamp(steps[i].get("timestamp"))
        prev_ts = _parse_timestamp(steps[i - 1].get("timestamp"))
        if curr_ts is not None and prev_ts is not None:
            steps[i]["duration_ms"] = max(0, int((curr_ts - prev_ts) * 1000))
        else:
            steps[i]["duration_ms"] = None

    # Build timestamps
    started_at = _timestamp_to_iso(events[0].get("timestamp")) if events else None
    ended_at = _timestamp_to_iso(events[-1].get("timestamp")) if events else None

    # Determine status
    status = "completed" if events else "empty"

    # Build summary
    user_messages = sum(1 for s in steps if s["type"] == "user_input")
    agent_responses = sum(1 for s in steps if s["type"] == "agent_response")
    tool_calls = sum(1 for s in steps if s["type"] == "tool_call")
    handoffs = sum(1 for s in steps if s["type"] == "handoff")
    thinking_steps = sum(1 for s in steps if s["type"] == "thinking")

    summary = {
        "total_steps": len(steps),
        "user_messages": user_messages,
        "agent_responses": agent_responses,
        "tool_calls": tool_calls,
        "handoffs": handoffs,
        "thinking_steps": thinking_steps,
        "total_tokens": total_input_tokens + total_output_tokens,
        "thoughts_tokens": total_thoughts_tokens,
        "cached_tokens": total_cached_tokens,
        "agents_involved": sorted(agents_involved),
    }

    return {
        "session_id": session_id,
        "agent_name": agent_name,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": status,
        "summary": summary,
        "steps": steps,
    }


async def update_adk_agent_session(
    agent_name: str,
    user_id: str,
    session_id: str,
    data: Dict[str, Any],
    base_url: str = None,
    token: str = None,
) -> Dict[str, Any]:
    """
    Update an agent session by making a PATCH request to the ADK endpoint.
    Args:
        agent_name: Name of the agent (e.g., "my_sample_agent")
        user_id: User ID (e.g., "u_123")
        session_id: Session ID (e.g., "s_123")
        data: Dictionary containing the request data
        base_url: Base url address (default: "localhost:8000")
        token: Bearer token for ADK authorization

    Returns:
        Dictionary containing the response data

    Raises:
        aiohttp.ClientError: If the HTTP request fails
        ValueError: If the response contains an error
    """
    if base_url is None:
        base_url = settings.root_path
    url = f"{base_url}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.patch(url, headers=headers, json=data) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"Failed to update agent session: {e}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse response JSON: {e}")


async def create_adk_agent_session(
    agent_name: str,
    user_id: str,
    session_id: str,
    data: Dict[str, Any],
    base_url: str = None,
    token: str = None,
) -> Dict[str, Any]:
    """
    Create an agent session by making a POST request to the ADK endpoint.
    Args:
        agent_name: Name of the agent (e.g., "my_sample_agent")
        user_id: User ID (e.g., "u_123")
        session_id: Session ID (e.g., "s_123")
        data: Dictionary containing the request data
        base_url: Base url address (default: "localhost:8000")
        token: Bearer token for ADK authorization

    Returns:
        Dictionary containing the response data

    Raises:
        aiohttp.ClientError: If the HTTP request fails
        ValueError: If the response contains an error
    """
    if base_url is None:
        base_url = settings.root_path
    url = f"{base_url}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    logger.info("Creating ADK Session started")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=data) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"Failed to create agent session: {e}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse response JSON: {e}")


async def delete_adk_agent_session(
    agent_name: str,
    user_id: str,
    session_id: str,
    base_url: str = None,
    token: str = None,
) -> Dict[str, Any]:
    """
    Delete an agent session by making a DELETE request to the ADK endpoint.
    Args:
        agent_name: Name of the agent (e.g., "my_sample_agent")
        user_id: User ID (e.g., "u_123")
        session_id: Session ID (e.g., "s_123")
        base_url: Base url address (default: "localhost:8000")
        token: Bearer token for ADK authorization

    Returns:
        Dictionary containing the response data

    Raises:
        aiohttp.ClientError: If the HTTP request fails
        ValueError: If the response contains an error
    """
    if base_url is None:
        base_url = settings.root_path
    url = f"{base_url}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.delete(url, headers=headers) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            raise aiohttp.ClientError(f"Failed to delete agent session: {e}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse response JSON: {e}")


# def run_agent_session_with_custom_data(
#     agent_name: str = "my_sample_agent",
#     user_id: str = "u_123",
#     session_id: str = "s_123",
#     data: Optional[Dict[str, Any]] = None,
#     base_url: str = "http//:localhost:8000",
# ) -> Dict[str, Any]:
#     """
#     Convenience function that runs an agent session with the exact data from the curl example.

#     Args:
#         agent_name: Name of the agent (default: "my_sample_agent")
#         user_id: User ID (default: "u_123")
#         session_id: Session ID (default: "s_123")
#         data: Custom data dictionary. If None, uses the example data {"key1": "value1", "key2": 42}
#         host: Host address (default: "localhost")
#         port: Port number (default: 8000)

#     Returns:
#         Dictionary containing the response data
#     """
#     if data is None:
#         data = {"key1": "value1", "key2": 42}

#     return run_adk_agent(agent_name, user_id, session_id, data, base_url)
