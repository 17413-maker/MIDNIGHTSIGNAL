"""
tools.py — Midnight Signal agent tools.

Each tool is a plain Python function plus a JSON-schema entry (Ollama /
OpenAI-style "tools" format) so agent.py can hand them straight to
ollama.chat(..., tools=TOOL_SCHEMAS).

Write/edit/run operations are gated by a confirm_fn callback supplied by
the caller (the CLI), so the UI layer controls how confirmation looks —
tools.py itself has no knowledge of terminal colors or prompts.
"""

import os
import re
import subprocess
import difflib

MAX_READ_BYTES = 200_000
MAX_LIST_ENTRIES = 200


class ToolError(Exception):
    pass


# Every user-declined action returns this prefix, so agent.py can recognise it
# (and remember it, so the model can't just retry the same call in a loop).
CANCELLED_PREFIX = "CANCELLED_BY_USER"


def cancelled(what: str) -> str:
    return (
        f"{CANCELLED_PREFIX}: the user declined to {what}. Nothing was changed. "
        "Do NOT retry this or an equivalent action. Acknowledge briefly and ask what they'd like instead."
    )


def _safe_path(path: str, root: str) -> str:
    """Resolve a user-given path against root and refuse to leave root's tree."""
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, path)) if not os.path.isabs(path) else os.path.abspath(path)
    if os.path.commonpath([root, target]) != root:
        raise ToolError(f"path '{path}' escapes the working directory ({root}) — refused")
    return target


# ─────────────────────────────────────────────────────────── read_file

def read_file(path: str, root: str) -> str:
    full = _safe_path(path, root)
    if not os.path.isfile(full):
        raise ToolError(f"no such file: {path}")
    size = os.path.getsize(full)
    if size > MAX_READ_BYTES:
        raise ToolError(f"file too large to read whole ({size} bytes): {path}")
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    numbered = "\n".join(f"{i+1:>5} | {line}" for i, line in enumerate(content.splitlines()))
    return numbered or "(empty file)"


# ─────────────────────────────────────────────────────────── write_file

def write_file(path: str, content: str, root: str, confirm_fn=None) -> str:
    full = _safe_path(path, root)
    exists = os.path.isfile(full)
    preview = content if len(content) < 2000 else content[:2000] + "\n...(truncated preview)..."
    if confirm_fn is not None:
        action = "overwrite" if exists else "create"
        if not confirm_fn(f"{action} file", path, preview):
            return cancelled(f"write '{path}'")
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


# ─────────────────────────────────────────────────────────── edit_file

def edit_file(path: str, search: str, replace: str, root: str, confirm_fn=None) -> str:
    full = _safe_path(path, root)
    if not os.path.isfile(full):
        raise ToolError(f"no such file: {path}")
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        original = f.read()

    count = original.count(search)
    if count == 0:
        raise ToolError(f"search text not found in {path} — edit refused")
    if count > 1:
        raise ToolError(
            f"search text matches {count} places in {path} — make it unique before editing"
        )

    updated = original.replace(search, replace, 1)

    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(), updated.splitlines(),
            fromfile=path, tofile=path, lineterm="",
        )
    )

    if confirm_fn is not None:
        if not confirm_fn("edit file", path, diff or "(no visible diff)"):
            return cancelled(f"edit '{path}'")

    with open(full, "w", encoding="utf-8") as f:
        f.write(updated)
    return f"edited {path} ({count} replacement applied)"


# ─────────────────────────────────────────────────────────── search_files

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_FILE_BYTES = 2_000_000


def search_files(query: str, path: str, root: str, max_results: int = MAX_SEARCH_RESULTS) -> str:
    """Grep-like recursive text/regex search under `path`, returns file:line: text hits."""
    full_root = _safe_path(path or ".", root)
    if not os.path.isdir(full_root):
        raise ToolError(f"no such directory: {path or '.'}")
    try:
        pattern = re.compile(query)
    except re.error as e:
        raise ToolError(f"bad search pattern: {e}")

    hits = []
    for dirpath, dirnames, filenames in os.walk(full_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(fpath) > MAX_SEARCH_FILE_BYTES:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if pattern.search(line):
                            rel = os.path.relpath(fpath, root)
                            hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                            if len(hits) >= max_results:
                                break
            except OSError:
                continue
            if len(hits) >= max_results:
                break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"no matches for {query!r} under {path or '.'}"
    suffix = f"\n...(capped at {max_results} results)" if len(hits) >= max_results else ""
    return "\n".join(hits) + suffix


# ─────────────────────────────────────────────────────────── list_dir

def list_dir(path: str, root: str) -> str:
    full = _safe_path(path or ".", root)
    if not os.path.isdir(full):
        raise ToolError(f"no such directory: {path or '.'}")
    entries = sorted(os.listdir(full))[:MAX_LIST_ENTRIES]
    lines = []
    for name in entries:
        p = os.path.join(full, name)
        tag = "/" if os.path.isdir(p) else ""
        size = "" if tag else f"  ({os.path.getsize(p)}b)"
        lines.append(f"{name}{tag}{size}")
    return "\n".join(lines) if lines else "(empty directory)"


# ─────────────────────────────────────────────────────────── run_command

DANGEROUS_PATTERNS = ["rm -rf /", ":(){ :|:& };:", "mkfs", "> /dev/sda", "dd if=/dev/zero"]


def run_command(command: str, root: str, confirm_fn=None, yolo: bool = False) -> str:
    for pat in DANGEROUS_PATTERNS:
        if pat in command:
            raise ToolError(f"refused: command matches a known-dangerous pattern ({pat!r})")

    if not yolo and confirm_fn is not None:
        if not confirm_fn("run shell command", command, command):
            return cancelled(f"run `{command}`")

    try:
        result = subprocess.run(
            command, shell=True, cwd=root,
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "command timed out after 30s"

    out = result.stdout.strip()
    err = result.stderr.strip()
    parts = [f"exit code: {result.returncode}"]
    if out:
        parts.append(f"stdout:\n{out[:4000]}")
    if err:
        parts.append(f"stderr:\n{err[:4000]}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────── git tools

def git_status(root: str) -> str:
    return run_command("git status --short --branch", root, confirm_fn=None, yolo=True)


def git_diff(root: str, path: str = "") -> str:
    cmd = f"git diff -- {path}" if path else "git diff"
    return run_command(cmd, root, confirm_fn=None, yolo=True)


# ────────────────────────────────────────── schemas for ollama tool-calling

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file's contents, with line numbers, relative to the current working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path relative to the working directory."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a file or fully overwrite an existing one with new content. Requires user confirmation unless yolo mode is on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the working directory."},
                    "content": {"type": "string", "description": "Full new content of the file."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Precisely edit a file by replacing one unique occurrence of `search` text with `replace` text. The search text must match exactly once in the file. Requires user confirmation unless yolo mode is on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "search": {"type": "string", "description": "Exact text to find, must be unique in the file."},
                    "replace": {"type": "string", "description": "Text to replace it with."},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Recursively search text/regex `query` across files under `path` (default '.') relative to the working directory. Returns matching file:line: text hits, read-only, no confirmation needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text or regex pattern to search for."},
                    "path": {"type": "string", "description": "Directory to search under, default '.'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders inside a directory relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path, default '.'"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the working directory and return stdout/stderr/exit code. Requires user confirmation unless yolo mode is on. Refused for known-destructive patterns.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show a short git status of the working directory (no confirmation needed, read-only).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show the git diff for the working directory or a specific path (no confirmation needed, read-only).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Optional path to scope the diff to."}},
                "required": [],
            },
        },
    },
]

TOOL_DESCRIPTIONS = [(s["function"]["name"], s["function"]["description"]) for s in TOOL_SCHEMAS]
