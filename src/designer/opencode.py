"""Drive opencode headlessly to produce or revise one design artifact.

Ported from builder_agent's executor, with two changes:

  * context is attached with `-f/--file` rather than inlined into argv. The
    design system + template can run to ~17k tokens, and files keep the command
    line sane while still putting the content in the model's message.
  * revision is a second turn on the SAME session (`-s <id>`), so the model does
    not re-read the whole design system to fix one contrast ratio.

`--auto` auto-approves edits so the run is non-interactive. Backend is llama.cpp
on :8500 via the opencode provider config -- no cloud, offline-capable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
MODEL = os.environ.get("DESIGNER_MODEL", "local-llama/gemma-4")


def list_files(workdir: str, since: set | None = None) -> list[str]:
    """Non-empty, non-hidden files under workdir (optionally only new ones)."""
    hits = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for f in files:
            if f.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(root, f), workdir)
            if since is not None and rel in since:
                continue
            if os.path.getsize(os.path.join(root, f)) > 0:
                hits.append(rel)
    return sorted(hits)


def _session_id(stdout: str) -> str | None:
    """opencode emits JSON events with --format json; recover the session id so
    a revision can continue the same conversation."""
    for m in re.finditer(r'"sessionID"\s*:\s*"([^"]+)"', stdout):
        return m.group(1)
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for k in ("sessionID", "sessionId", "session_id"):
            if isinstance(d, dict) and d.get(k):
                return d[k]
    return None


def run(prompt: str, workdir: str, files: list[str] | None = None,
        session: str | None = None, model: str = MODEL,
        timeout: float = 900) -> tuple[list[str], str, str | None]:
    """One opencode turn. Returns (new_files, log, session_id)."""
    os.makedirs(workdir, exist_ok=True)
    before = set(list_files(workdir))
    cmd = [OPENCODE, "run", prompt, "--auto", "-m", model,
           "--dir", workdir, "--format", "json"]
    if session:
        cmd += ["-s", session]
    for f in (files or []):
        if os.path.isfile(f):
            cmd += ["-f", f]
    env = {**os.environ,
           "PATH": os.path.dirname(OPENCODE) + ":" + os.environ.get("PATH", "")}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    return list_files(workdir, since=before), out[-4000:], _session_id(out)


def run_with_retry(prompt: str, workdir: str, retries: int = 2,
                   **kw) -> tuple[list[str], str, str | None]:
    """Retry on no-output. The builder agent found this genuinely necessary --
    the model sometimes returns having written nothing at all."""
    last = ([], "", None)
    for _ in range(retries + 1):
        files, log, sid = run(prompt, workdir, **kw)
        if files:
            return files, log, sid
        last = (files, log, sid)
    return last
