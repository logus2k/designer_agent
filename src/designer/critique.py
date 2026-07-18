"""Tier 3: visual critique of a rendered screenshot.

Measured behaviour of this tier, from a good/bad fixture pair:

  * It genuinely sees the page -- it reads back real headline and button text.
  * It caught a contrast failure that the deterministic tiers rank differently.
  * It MISSED both geometry defects (a headline overflowing its box, and an
    overlapping badge) that tier 2 catches trivially.
  * Asked to be a harsh reviewer, it returned "needs_work" for a clean page too,
    inventing a P0 about the design being "generic" -- despite being told to say
    so if the page was fine.

So the verdict field carries no information and this tier NEVER gates. What does
discriminate is evidence: real findings name an element and a measurable
property, invented ones are aesthetic adjectives. keep_evidence_bearing() filters
on exactly that.

Note the model emits reasoning into a separate `reasoning_content` field, and
that reasoning shares the max_tokens budget -- too small a budget returns empty
content with no error.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request

ENDPOINT = "http://127.0.0.1:8500/v1/chat/completions"
MODEL = "gemma-4"
# reasoning + answer share this budget; 900 was empirically too small
MAX_TOKENS = 2000

PROMPT = """You are reviewing a screenshot of a web page rendered at {w}x{h}.

The page is meant to be: {intent}

Identify concrete VISUAL problems a user would notice. For each one you MUST name
the specific element you can see and what is wrong with it. Do not comment on
things you cannot see in the image. If the page looks correct, return an empty
issues list -- an empty list is a valid and expected answer.

Do NOT report: generic taste opinions ("looks dated", "feels generic",
"forgettable"), missing features, or anything you are inferring rather than
seeing.

Return ONLY JSON:
{{"issues":[{{"element":"<what you see>","problem":"<what is wrong>",
"severity":"P1"|"P2"}}]}}"""

# An issue is evidence-bearing when it points at something concrete rather than
# passing aesthetic judgement.
_VAGUE = re.compile(
    r"\b(generic|forgettable|dated|bland|boring|uninspired|modern|clean|"
    r"professional|polished|personality|emotional|memorable|template|"
    r"2018|2019|feels?|vibe|aesthetic)\b", re.I)


def _extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"\{.*\}", text, re.S)
        raw = m.group(0) if m else None
    if raw is None:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def critique(png_path: str, intent: str = "a clean, readable web page",
             viewport: tuple[int, int] = (1440, 900),
             timeout: float = 300) -> dict:
    """Ask the vision model what is visibly wrong. Never returns a verdict."""
    with open(png_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    body = {
        "model": MODEL, "temperature": 0.2, "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": [
            {"type": "text",
             "text": PROMPT.format(w=viewport[0], h=viewport[1], intent=intent)},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    msg = data["choices"][0]["message"]
    parsed = _extract_json(msg.get("content") or "")
    return {
        "issues": parsed.get("issues", []) if isinstance(parsed, dict) else [],
        "raw": (msg.get("content") or "")[:2000],
        "truncated": data["choices"][0].get("finish_reason") == "length",
        "usage": data.get("usage", {}),
    }


def keep_evidence_bearing(issues: list[dict]) -> list[dict]:
    """Drop issues that assert taste instead of pointing at something visible."""
    out = []
    for i in issues:
        if not isinstance(i, dict):
            continue
        el = str(i.get("element", "")).strip()
        prob = str(i.get("problem", "")).strip()
        if not el or not prob:
            continue
        if _VAGUE.search(prob) or _VAGUE.search(el):
            continue
        out.append(i)
    return out


def as_findings(issues: list[dict]) -> list[dict]:
    """Shape surviving issues like verify.py findings, always non-gating (P2)."""
    return [{"severity": "P2", "rule": "visual",
             "message": f'{i.get("element")}: {i.get("problem")}'}
            for i in issues]
