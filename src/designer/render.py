"""Render an HTML artifact headlessly and extract a geometry audit.

Drives the Chromium that ships in the Playwright browser cache directly via its
CLI -- no playwright package, no CDP client, no websocket. The audit probe
(assets/audit.js) is injected into a copy of the artifact, runs in page context,
and stashes its JSON in a <script> tag that `--dump-dom` carries back to us.

Two invocations per artifact: one for the audit DOM, one for the screenshot
(Chromium does not reliably do both in a single headless run).
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_JS = os.path.join(_HERE, "assets", "audit.js")

VIEWPORT = (1440, 900)
VIRTUAL_TIME_MS = 3000


# Search order, later entries win (playwright builds sort by build number, and a
# newer build is preferred). Module-level so tests can substitute it -- hardcoding
# these inside the function made the fallback branches impossible to exercise.
CHROME_CANDIDATES = [
    "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
]


def find_chrome(candidates: list[str] | None = None) -> str:
    """Locate a headless-capable Chromium. Explicit env wins, then search order."""
    env = os.environ.get("DESIGNER_CHROME")
    if env:
        if not os.path.exists(env):
            raise RuntimeError(f"DESIGNER_CHROME={env} does not exist")
        return env
    hits = []
    for pat in (candidates if candidates is not None else CHROME_CANDIDATES):
        hits.extend(sorted(glob.glob(os.path.expanduser(pat))))
    if not hits:
        raise RuntimeError("no Chromium found; set DESIGNER_CHROME to a browser binary")
    return hits[-1]


_BASE_FLAGS = [
    "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
    "--disable-dev-shm-usage", "--force-color-profile=srgb",
    "--disable-lcd-text", "--deterministic-mode",
]


def _instrument(html: str, config: dict) -> str:
    """Append the probe before </body>. Scripts don't affect layout, so the
    instrumented copy lays out identically to the original."""
    with open(AUDIT_JS, encoding="utf-8") as fh:
        probe = fh.read()
    # An HTML parser ends a <script> at the first "</script" regardless of JS
    # context -- even inside a comment or string. Escape it in both payloads.
    probe = re.sub(r"</(script)", r"<\\/\1", probe, flags=re.I)
    cfg = json.dumps(config).replace("</", "<\\/")
    block = f"<script>window.__OD_AUDIT__ = {cfg};</script>\n<script>\n{probe}\n</script>\n"
    m = re.search(r"</body\s*>", html, re.I)
    if m:
        return html[: m.start()] + block + html[m.start():]
    return html + block


def audit(html_path: str, palette: list[str] | None = None,
          components: list[str] | None = None,
          viewport: tuple[int, int] = VIEWPORT,
          timeout: float = 90) -> dict:
    """Render html_path and return the geometry audit as a dict."""
    chrome = find_chrome()
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    cfg = {"palette": palette or [], "components": components or []}
    work = tempfile.mkdtemp(prefix="od-audit-")
    inst = os.path.join(work, "instrumented.html")
    with open(inst, "w", encoding="utf-8") as fh:
        fh.write(_instrument(html, cfg))

    cmd = [chrome, *_BASE_FLAGS,
           f"--window-size={viewport[0]},{viewport[1]}",
           f"--virtual-time-budget={VIRTUAL_TIME_MS}",
           "--dump-dom", f"file://{inst}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    dom = r.stdout
    # Require the JSON mime and take the last match: the probe's own source text
    # mentions this tag id in a comment, so a looser pattern matches that first.
    hits = re.findall(
        r'<script[^>]*type="application/json"[^>]*id="__od_audit__"[^>]*>(.*?)</script>',
        dom, re.S)
    if not hits:
        raise RuntimeError(
            f"audit probe produced no output (chrome rc={r.returncode}); "
            f"stderr tail: {r.stderr[-400:]}")
    return json.loads(hits[-1].replace("\\u003c", "<"))


def doc_height(html_path: str, width: int = VIEWPORT[0], timeout: float = 90) -> int:
    """Full rendered content height at the given width.

    Headless Chromium's CLI has no working full-page screenshot flag, so a
    full-page capture is really 'measure the content, then size the window to
    it'. This is that measurement.
    """
    chrome = find_chrome()
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    probe = ('<script>(()=>{const t=document.createElement("script");'
             't.type="application/json";t.id="__od_height__";'
             't.textContent=JSON.stringify({h:Math.max('
             'document.documentElement.scrollHeight,document.body.scrollHeight)});'
             'document.body.appendChild(t);})();</script>')
    work = tempfile.mkdtemp(prefix="od-height-")
    inst = os.path.join(work, "h.html")
    m = re.search(r"</body\s*>", html, re.I)
    with open(inst, "w", encoding="utf-8") as fh:
        fh.write(html[: m.start()] + probe + html[m.start():] if m else html + probe)
    r = subprocess.run(
        [chrome, *_BASE_FLAGS, f"--window-size={width},{VIEWPORT[1]}",
         f"--virtual-time-budget={VIRTUAL_TIME_MS}", "--dump-dom", f"file://{inst}"],
        capture_output=True, text=True, timeout=timeout)
    hits = re.findall(
        r'<script[^>]*type="application/json"[^>]*id="__od_height__"[^>]*>(.*?)</script>',
        r.stdout, re.S)
    if not hits:
        raise RuntimeError(f"could not measure document height for {html_path}")
    return int(json.loads(hits[-1])["h"])


def screenshot(html_path: str, out_png: str,
               viewport: tuple[int, int] = VIEWPORT,
               full_page: bool = False, timeout: float = 90) -> str:
    """Render html_path to a PNG for the vision-critique tier.

    full_page=True measures the content height first and sizes the window to it;
    there is no Chromium CLI flag that does this (--full-page-heuristics is
    silently ignored and yields a viewport-sized image).
    """
    chrome = find_chrome()
    w, h = viewport
    if full_page:
        h = max(h, min(doc_height(html_path, w, timeout=timeout), 20000))
    cmd = [chrome, *_BASE_FLAGS,
           f"--window-size={w},{h}",
           f"--virtual-time-budget={VIRTUAL_TIME_MS}",
           f"--screenshot={out_png}",
           f"file://{os.path.abspath(html_path)}"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if not os.path.exists(out_png):
        raise RuntimeError(f"screenshot not produced for {html_path}")
    return out_png
