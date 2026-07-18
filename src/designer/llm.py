"""Direct generation against the local llama.cpp server.

Why this exists alongside opencode.py, measured rather than assumed:

opencode drives the model through a tool-calling loop, and its system prompt plus
tool schemas cost ~11k tokens before any of our content. With the design system
and template attached, input reached 18,383 tokens against a 32,768-token slot,
and gemma-4 stopped emitting real tool calls -- it printed the call as plain text
(`[read{filePath: "..."}]`) and halted without writing anything. The same model
writes files correctly through opencode when the prompt is short.

So for artifact generation we skip the tool loop entirely: put the context in the
prompt, have the model emit one complete HTML document as text, and write the
file ourselves. This is the shape open-design calls the "text-artifact execution
profile" for plain-stream runtimes, and it removes tool-calling as a failure mode
while freeing ~11k tokens of budget.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

ENDPOINT = "http://127.0.0.1:8500/v1/chat/completions"
MODEL = "gemma-4"
# Sized to the slot, not to caution. The server reports n_ctx 32768 per slot
# (--ctx-size 65536 / --parallel 2) against a model trained to 131072, so the
# real ceiling is a config choice upstream. Overridable, and it should be raised
# alongside any --parallel or --ctx-size change rather than left behind.
SLOT_CONTEXT = int(os.environ.get("DESIGNER_SLOT_CONTEXT", 32768))
# Output is sized to the ARTIFACT, not the slot -- these scale differently.
# A full page is 12-20KB (~3-5k tokens); 16384 is ~3x headroom. Scaling this
# with the slot actively hurt: at 32768 a revision turn burned 33k completion
# tokens on reasoning and never closed the document. Input budget should track
# the slot; output should not.
MAX_TOKENS = int(os.environ.get("DESIGNER_MAX_TOKENS", 16384))

_FENCE = re.compile(r"```(?:html)?\s*(.*?)```", re.S)
_DOC = re.compile(r"(<!DOCTYPE html.*?</html\s*>)", re.S | re.I)
_BARE = re.compile(r"(<html[^>]*>.*?</html\s*>)", re.S | re.I)


def extract_html(text: str) -> str | None:
    """Pull one complete HTML document out of a model response.

    Tried in order of confidence: a full doctype-to-</html> document anywhere in
    the text, then a fenced block, then a bare <html> element. Prose around the
    artifact is expected and ignored.
    """
    m = _DOC.search(text)
    if m:
        return m.group(1).strip()
    for block in _FENCE.findall(text):
        b = block.strip()
        if "<html" in b.lower() or "<!doctype" in b.lower():
            return b
    m = _BARE.search(text)
    if m:
        return m.group(1).strip()
    return None


def complete(prompt: str, system: str | None = None,
             max_tokens: int = MAX_TOKENS, temperature: float = 0.3,
             timeout: float = 900) -> dict:
    """One completion. Returns {text, reasoning, usage, truncated}."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": MODEL, "messages": msgs, "max_tokens": max_tokens,
            "temperature": temperature}
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.URLError as e:
        raise RuntimeError(f"local model unreachable at {ENDPOINT}: {e}") from e
    choice = data["choices"][0]
    msg = choice["message"]
    return {
        "text": msg.get("content") or "",
        # reasoning shares the token budget; surfaced so a truncated run is
        # diagnosable rather than just returning empty content
        "reasoning": msg.get("reasoning_content") or "",
        "usage": data.get("usage", {}),
        "truncated": choice.get("finish_reason") == "length",
    }


def generate_html(prompt: str, system: str | None = None,
                  max_tokens: int = MAX_TOKENS,
                  temperature: float = 0.3,
                  timeout: float = 900) -> tuple[str | None, dict]:
    """Generate one HTML artifact. Returns (html_or_None, meta)."""
    r = complete(prompt, system=system, max_tokens=max_tokens,
                 temperature=temperature, timeout=timeout)
    html = extract_html(r["text"])
    meta = {"usage": r["usage"], "truncated": r["truncated"],
            "reasoning_chars": len(r["reasoning"]),
            "response_chars": len(r["text"])}
    if html is None and r["truncated"]:
        meta["error"] = "output hit the token limit before the document closed"
    elif html is None:
        meta["error"] = "no HTML document found in the response"
        meta["head"] = r["text"][:400]
    return html, meta
