"""
radar.py — the /radar project scanner.

Sweeps a directory tree (stdlib only) and produces a dashboard: language
breakdown by lines of code, hottest (largest) files, recently touched
files, TODO/FIXME/HACK markers, and git state.
"""

import os
import re
import subprocess
import time
from collections import Counter

import ui

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist",
    "build", ".next", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    "target", ".cache", ".tox", ".gradle", "vendor",
}

LANGS = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".sh": "Shell", ".bash": "Shell", ".html": "HTML",
    ".css": "CSS", ".scss": "CSS", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML",
    ".toml": "TOML", ".md": "Markdown", ".txt": "Text", ".sql": "SQL", ".lua": "Lua",
}

MARKER_RE = re.compile(r"(?:#|//|/\*|<!--|--|;)\s*(TODO|FIXME|HACK|XXX)\b[:\s(-]*(.*)")
MAX_FILES = 8000
MAX_TEXT_BYTES = 500_000
MAX_MARKERS = 200


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def _ago(ts: float) -> str:
    d = max(time.time() - ts, 0)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _git(root: str, *args: str):
    try:
        out = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, timeout=4
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def scan(root: str) -> dict:
    lang_lines = Counter()
    lang_files = Counter()
    biggest = []
    recent = []
    markers = []
    marker_total = 0
    nfiles = 0
    total_bytes = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            nfiles += 1
            if nfiles > MAX_FILES:
                truncated = True
                break
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root)
            total_bytes += st.st_size
            biggest.append((st.st_size, rel))
            recent.append((st.st_mtime, rel))

            lang = LANGS.get(os.path.splitext(fn)[1].lower())
            if not lang or st.st_size > MAX_TEXT_BYTES:
                if lang:
                    lang_files[lang] += 1
                continue
            lang_files[lang] += 1
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        lang_lines[lang] += 1
                        if "TODO" in line or "FIXME" in line or "HACK" in line or "XXX" in line:
                            m = MARKER_RE.search(line)
                            if m:
                                marker_total += 1
                                if len(markers) < MAX_MARKERS:
                                    markers.append((rel, lineno, m.group(1), m.group(2).strip()[:70]))
            except OSError:
                continue
        if truncated:
            break

    biggest.sort(reverse=True)
    recent.sort(reverse=True)

    git = None
    if _git(root, "rev-parse", "--is-inside-work-tree") == "true":
        status = _git(root, "status", "--porcelain") or ""
        git = {
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "?",
            "dirty": len([l for l in status.splitlines() if l.strip()]),
            "last": _git(root, "log", "-1", "--format=%h %s (%cr)") or "no commits yet",
        }

    return {
        "root": root, "files": nfiles if not truncated else MAX_FILES, "bytes": total_bytes,
        "truncated": truncated, "lang_lines": lang_lines, "lang_files": lang_files,
        "biggest": biggest[:5], "recent": recent[:5], "markers": markers,
        "marker_total": marker_total, "git": git,
    }


def render(data: dict) -> str:
    out = [""]
    title = "◆ PROJECT RADAR"
    out.append("  " + ui.gradient_text(title, bold=True))
    out.append("  " + ui.gray(data["root"]))
    out.append("")

    trunc = ui.gray("  (capped)") if data["truncated"] else ""
    total_lines = sum(data["lang_lines"].values())
    out.append(
        "  " + ui.gray("files ") + ui.white(str(data["files"]))
        + ui.gray("   size ") + ui.white(_human_size(data["bytes"]))
        + ui.gray("   lines ") + ui.white(f"{total_lines:,}") + trunc
    )

    if data["git"]:
        g = data["git"]
        state = ui.success("clean") if g["dirty"] == 0 else ui.warn(f"{g['dirty']} changed")
        out.append("  " + ui.gray("git   ") + ui.solid(g["branch"], ui.gradient_color(0.3), bold=True) + "  " + state)
        out.append("  " + ui.gray("last  ") + ui.white(g["last"]))
    out.append("")

    if data["lang_lines"] or data["lang_files"]:
        out.append("  " + ui.solid("LANGUAGES", ui.gradient_color(0.1), bold=True))
        out.append("")
        ranked = sorted(
            data["lang_files"].keys(),
            key=lambda l: (data["lang_lines"][l], data["lang_files"][l]),
            reverse=True,
        )[:8]
        top = max((data["lang_lines"][l] for l in ranked), default=0) or 1
        for lang in ranked:
            lines = data["lang_lines"][lang]
            nfiles = data["lang_files"][lang]
            bar = ui.gradient_bar(lines / top, 22)
            detail = f"{lines:,} lines · {nfiles} files"
            out.append(f"  {ui.white(lang.ljust(12))}{bar}  {ui.gray(detail)}")
        out.append("")

    if data["biggest"]:
        out.append("  " + ui.solid("HEAVIEST FILES", ui.gradient_color(0.4), bold=True))
        out.append("")
        for size, rel in data["biggest"]:
            out.append(f"  {ui.gray(_human_size(size).rjust(8))}  {ui.white(rel)}")
        out.append("")

    if data["recent"]:
        out.append("  " + ui.solid("RECENTLY TOUCHED", ui.gradient_color(0.6), bold=True))
        out.append("")
        for ts, rel in data["recent"]:
            out.append(f"  {ui.gray(_ago(ts).rjust(8))}  {ui.white(rel)}")
        out.append("")

    out.append("  " + ui.solid(f"MARKERS  ({data['marker_total']})", ui.gradient_color(0.85), bold=True))
    out.append("")
    if data["markers"]:
        for rel, lineno, kind, text in data["markers"][:8]:
            out.append(f"  {ui.warn(kind.ljust(6))} {ui.gray(f'{rel}:{lineno}')}  {ui.white(text)}")
        if data["marker_total"] > 8:
            out.append("  " + ui.gray(f"… and {data['marker_total'] - 8} more"))
    else:
        out.append("  " + ui.gray("no TODO / FIXME / HACK markers found"))
    out.append("")
    return "\n".join(out)
