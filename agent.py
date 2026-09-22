"""
agent.py — Midnight Signal agent loop.

Drives a read -> reason -> act -> verify cycle on top of ollama.chat's
tool-calling support. Each tool call is dispatched to tools.py, results
are fed back as tool-role messages, and the loop continues until the
model stops requesting tools or a max-step budget is hit.
"""

import json
import re

import ollama

import tools as tool_impl

MAX_STEPS = 8

# Tool-call turns need to be deterministic and well-formed, not creative —
# the Modelfile's temperature (0.7) is tuned for normal chat and makes the
# model more likely to drift out of the structured tool-calling format.
_AGENT_OPTIONS = {"temperature": 0.15, "top_p": 0.85, "repeat_penalty": 1.1}

# ──────────────────────────────────────────────────────────── anti-hallucination guard
#
# Small/local models sometimes skip tool calling entirely and answer a
# file/command/git question straight out of the training-data prior (i.e.
# they hallucinate plausible-looking content instead of actually reading
# it). If the user's message looks like a request to inspect real state
# and the model answered without calling *any* tool this turn, we nudge it
# back on track once before accepting the answer.

# ──────────────────────────────────────────────────────────── anti-hallucination guard
#
# Small/local models sometimes skip tool calling entirely and answer a
# file/command/git question straight out of the training-data prior (i.e.
# they hallucinate plausible-looking content instead of actually reading
# it). If the user's message looks like a request to inspect or change real
# state and the model answered without calling *any* tool this turn, we
# nudge it back on track once before accepting the answer.

_TOOL_INTENT_VERB_RE = re.compile(
    r"\b(read|open|show|display|print(?:\s+out)?|cat|view|check|inspect|look\s*at|"
    r"what'?s\s+in|what\s+does\s+.*\s+(?:say|contain)|contents?\s+of|list|ls|"
    r"diff|status|create|write|make|generate|edit|modify|update|change|delete|remove|run|execute|"
    r"search|find|grep|locate)\b",
    re.IGNORECASE,
)
_TOOL_INTENT_HINT_RE = re.compile(
    r"[.\w/\\-]+\.[A-Za-z0-9]{1,6}\b|\bfile\b|\bdirectory\b|\bfolder\b|\bgit\b|\bcommand\b",
    re.IGNORECASE,
)


def _looks_like_tool_intent(text: str) -> bool:
    """Heuristic: does this message ask the agent to look at or change real project state?"""
    if not text:
        return False
    return bool(_TOOL_INTENT_VERB_RE.search(text)) and bool(_TOOL_INTENT_HINT_RE.search(text))


_GUARD_NUDGE = (
    "You just answered without calling any tool this turn. If your answer stated or implied "
    "the contents of a file, a directory listing, command output, or git output — or claimed "
    "you created, edited, ran, or found something — that was fabricated: nothing actually "
    "happened. Never guess file/command/git/search output, and never say you did something "
    "without a matching tool call. Call the appropriate tool now (read_file, write_file, "
    "edit_file, list_dir, search_files, run_command, git_status, or git_diff) and answer again "
    "using its real result. If the user's request genuinely needs no tool, answer plainly instead."
)

TOOL_DISPATCH = {
    "read_file": lambda args, root, confirm_fn, yolo: tool_impl.read_file(args["path"], root),
    "write_file": lambda args, root, confirm_fn, yolo: tool_impl.write_file(
        args["path"], args.get("content", ""), root, confirm_fn=None if yolo else confirm_fn
    ),
    "edit_file": lambda args, root, confirm_fn, yolo: tool_impl.edit_file(
        args["path"], args["search"], args["replace"], root, confirm_fn=None if yolo else confirm_fn
    ),
    "list_dir": lambda args, root, confirm_fn, yolo: tool_impl.list_dir(args.get("path", "."), root),
    "search_files": lambda args, root, confirm_fn, yolo: tool_impl.search_files(
        args["query"], args.get("path", "."), root
    ),    "run_command": lambda args, root, confirm_fn, yolo: tool_impl.run_command(
        args["command"], root, confirm_fn=confirm_fn, yolo=yolo
    ),
    "git_status": lambda args, root, confirm_fn, yolo: tool_impl.git_status(root),
    "git_diff": lambda args, root, confirm_fn, yolo: tool_impl.git_diff(root, args.get("path", "")),
}


def _run_tool(name: str, args: dict, root: str, confirm_fn, yolo: bool) -> str:
    dispatch = TOOL_DISPATCH.get(name)
    if dispatch is None:
        return f"error: unknown tool '{name}'"
    try:
        return dispatch(args, root, confirm_fn, yolo)
    except tool_impl.ToolError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: unexpected failure — {e}"


# ──────────────────────────────────────────────────────── fake tool-call recovery
#
# Some local models/quantizations don't reliably use Ollama's structured
# tool-calling channel — instead they print something that *looks* like a
# tool call (often a fenced ```json block) as plain assistant text. When
# that happens `msg["tool_calls"]` is empty, so nothing actually runs, even
# though the model (and the user reading the transcript) believes it did.
# We scan the text for an embedded call and, if it matches a real tool,
# execute it for real instead of letting the fake output stand unchallenged.

def _iter_json_objects(text: str):
    """Yield every JSON object embedded in free text. Uses the real JSON decoder,
    so braces inside string values (e.g. code in write_file content) can't
    confuse it the way naive brace-counting could."""
    decoder = json.JSONDecoder()
    i = 0
    while True:
        i = text.find("{", i)
        if i == -1:
            return
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        yield obj
        i = end


def _payloads(obj):
    """Flatten the wrappers models like to invent — {"tool_calls": [...]},
    {"function": {...}}, or a bare list — into plain {"name", "arguments"} dicts."""
    if isinstance(obj, list):
        for item in obj:
            yield from _payloads(item)
    elif isinstance(obj, dict):
        if isinstance(obj.get("tool_calls"), list):
            yield from _payloads(obj["tool_calls"])
        elif isinstance(obj.get("function"), dict):
            yield obj["function"]
        else:
            yield obj


def _extract_fake_tool_call(text: str):
    """Find a tool call the model printed as plain text. Returns (name, args)
    for the first one that names a real tool, else None."""
    if not text:
        return None
    for obj in _iter_json_objects(text):
        for payload in _payloads(obj):
            name = payload.get("name")
            args = next((payload[k] for k in ("arguments", "parameters", "args", "input") if k in payload), None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    continue
            if name in TOOL_DISPATCH and isinstance(args, dict):
                return name, args
    return None


_REPEAT_DECLINED_TEXT = (
    "I was about to repeat an action you already declined, so I stopped. "
    "Tell me what you'd like me to do instead."
)


def _call_signature(name: str, args: dict):
    try:
        return name, json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return name, repr(args)


class AgentEvent:
    """Lightweight event object yielded during the loop, for the UI to render."""
    def __init__(self, kind: str, **data):
        self.kind = kind  # "tool_call" | "tool_result" | "final" | "guard_nudge" | "step_limit" | "error"
        self.data = data


def _execute(name, args, root, confirm_fn, yolo, declined, recovered=False):
    """Run one tool call, yielding the UI events for it. Returns the result text,
    or None if this exact call was already declined by the user this turn (in
    which case nothing is shown and nothing is asked again)."""
    sig = _call_signature(name, args)
    if sig in declined:
        return None
    yield AgentEvent("tool_call", name=name, args=args, recovered=recovered)
    result = _run_tool(name, args, root, confirm_fn, yolo)
    if result.startswith(tool_impl.CANCELLED_PREFIX):
        declined.add(sig)
    yield AgentEvent("tool_result", name=name, result=result)
    return result


def run_agent(model: str, history: list, root: str, confirm_fn=None, yolo: bool = False):
    """
    Runs the agent loop. `history` is the full message list (system + prior
    turns + the new user message already appended by the caller).

    Yields AgentEvent objects as it goes; the final event is always kind
    "final" carrying the assistant's closing text, which the caller should
    also append to history as an assistant message.
    """
    messages = list(history)

    # Text of the user message that kicked off this turn, for the guard heuristic.
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "") or ""
            break

    any_tool_called = False
    guard_used = False
    declined = set()          # (tool, args) pairs the user said no to this turn
    recovery_noted = False    # only flag "recovered from text" once per turn

    for step in range(MAX_STEPS):
        yield AgentEvent("thinking")
        try:
            response = ollama.chat(
                model=model,
                messages=messages,
                tools=tool_impl.TOOL_SCHEMAS,
                options=_AGENT_OPTIONS,
            )
        except Exception as e:
            yield AgentEvent("error", error=str(e))
            return

        msg = response["message"]
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            final_text = msg.get("content", "") or ""

            # The model may have "faked" a tool call as plain text instead of
            # using the real tool-calling channel — recover and actually run it.
            recovered = _extract_fake_tool_call(final_text)
            if recovered is not None:
                name, args = recovered
                any_tool_called = True
                result_text = yield from _execute(
                    name, args, root, confirm_fn, yolo, declined, recovered=not recovery_noted
                )
                recovery_noted = True
                if result_text is None:
                    yield AgentEvent("final", text=_REPEAT_DECLINED_TEXT)
                    return
                messages.append({"role": "assistant", "content": final_text})
                messages.append({"role": "tool", "content": result_text, "tool_name": name})
                continue

            if (
                not any_tool_called
                and not guard_used
                and _looks_like_tool_intent(user_text)
            ):
                guard_used = True
                messages.append(msg)
                messages.append({"role": "system", "content": _GUARD_NUDGE})
                yield AgentEvent("guard_nudge", reason=_GUARD_NUDGE)
                continue

            yield AgentEvent("final", text=final_text)
            return

        any_tool_called = True

        # Record the assistant's tool-call turn in the running message list
        messages.append(msg)

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = raw_args or {}

            result_text = yield from _execute(name, args, root, confirm_fn, yolo, declined)
            if result_text is None:
                yield AgentEvent("final", text=_REPEAT_DECLINED_TEXT)
                return

            messages.append({
                "role": "tool",
                "content": result_text,
                "tool_name": name,
            })

    yield AgentEvent("step_limit", steps=MAX_STEPS)
