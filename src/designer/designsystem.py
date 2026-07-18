"""Load and validate a design system.

A design system is selected by id in project config, so any of the ~151 bundled
packages can be chosen. That makes validation load-bearing: a package whose prose
contradicts its own tokens will hand the model two different palettes, and a
package whose fonts are not installed will render as something the vision tier
then critiques by mistake.

validate() is cheap and deterministic, so it can gate selection at config time
rather than failing halfway through a run.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import re
import subprocess

DATA_ROOT = os.environ.get(
    "DESIGNER_DATA",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data"))
SYSTEMS_DIR = os.path.abspath(os.path.join(DATA_ROOT, "design_systems"))

# Prose colour claims look like:  - **Primary:** `#0C0C09` — ...
_PROSE_COLOR = re.compile(
    r"^\s*-\s*\*\*(?P<role>[\w /&-]+?):?\*\*:?\s*`?(?P<hex>#[0-9a-fA-F]{3,8})`?", re.M)
_TOKEN = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
_FONT_STACK = re.compile(r"--font-[\w-]*\s*:\s*([^;]+);")

# Which token a prose role is expected to agree with.
_ROLE_TOKEN = {
    "primary": "--accent", "accent": "--accent",
    "surface": "--surface", "background": "--bg",
    "text": "--fg", "foreground": "--fg",
}


def _rgb(value: str) -> tuple[int, int, int] | None:
    v = value.strip()
    m = re.match(r"^#([0-9a-fA-F]{3,8})$", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) >= 6:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return None
    m = re.match(r"^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)", v)
    return tuple(int(m.group(i)) for i in (1, 2, 3)) if m else None


def _luminance(c: tuple[int, int, int]) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@functools.lru_cache(maxsize=1)
def installed_font_families() -> frozenset[str]:
    """Lowercased font families known to fontconfig. Empty if fc-list is absent."""
    try:
        r = subprocess.run(["fc-list", ":", "family"], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    fams = set()
    for line in r.stdout.splitlines():
        for part in line.split(","):
            if part.strip():
                fams.add(part.strip().lower())
    return frozenset(fams)


# Families every system can rely on without installation.
_GENERIC = {"system-ui", "sans-serif", "serif", "monospace", "ui-monospace",
            "ui-sans-serif", "ui-serif", "cursive", "fantasy", "-apple-system",
            "blinkmacsystemfont", "emoji", "math", "fangsong"}


@dataclasses.dataclass
class DesignSystem:
    id: str
    path: str
    design_md: str
    usage_md: str
    tokens_css: str
    tokens: dict[str, str]

    def palette(self) -> list[str]:
        """Concrete colour values, for the renderer's off-palette check."""
        out = []
        for v in self.tokens.values():
            if _rgb(v):
                out.append(v.strip())
        return sorted(set(out))

    def context_tokens(self) -> int:
        return (len(self.design_md) + len(self.usage_md) + len(self.tokens_css)) // 4

    def font_stacks(self) -> list[dict]:
        """Each --font-* declaration as {named: [...], generic: bool}.

        A stack is *designed* to degrade: entries like Menlo or SF Mono are
        macOS-only on purpose, and a trailing `monospace`/`sans-serif` is the
        author's explicit final fallback. So a stack ending in a generic never
        lands somewhere unintended -- only one with no generic terminator and no
        installed family does.
        """
        stacks = []
        for stack in _FONT_STACK.findall(self.tokens_css):
            if "var(" in stack:
                continue
            parts = [f.strip().strip('"\'').lower() for f in stack.split(",")]
            parts = [p for p in parts if p]
            named = [p for p in parts if p not in _GENERIC]
            if named:
                stacks.append({"named": named,
                               "generic": any(p in _GENERIC for p in parts)})
        return stacks

    def font_families(self) -> list[str]:
        return sorted({f for s in self.font_stacks() for f in s["named"]})


def available() -> list[str]:
    if not os.path.isdir(SYSTEMS_DIR):
        return []
    return sorted(d for d in os.listdir(SYSTEMS_DIR)
                  if os.path.isfile(os.path.join(SYSTEMS_DIR, d, "tokens.css")))


def load(ds_id: str) -> DesignSystem:
    path = os.path.join(SYSTEMS_DIR, ds_id)
    tpath = os.path.join(path, "tokens.css")
    if not os.path.isfile(tpath):
        raise FileNotFoundError(
            f"unknown design system '{ds_id}'; {len(available())} available under {SYSTEMS_DIR}")

    def rd(name: str) -> str:
        p = os.path.join(path, name)
        if not os.path.isfile(p):
            return ""
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    css = rd("tokens.css")
    return DesignSystem(id=ds_id, path=path, design_md=rd("DESIGN.md"),
                        usage_md=rd("USAGE.md"), tokens_css=css,
                        tokens={m.group(1): m.group(2).strip()
                                for m in _TOKEN.finditer(css)})


def validate(ds: DesignSystem, aa_normal: float = 4.5) -> list[dict]:
    """Deterministic defects in the design system itself.

    Severities match verify.py so both reports can be read together.
    """
    out = []

    # --- 1. prose vs tokens: both get injected, both claim authority ---
    for m in _PROSE_COLOR.finditer(ds.design_md):
        role = m.group("role").strip().lower()
        token = _ROLE_TOKEN.get(role)
        if not token or token not in ds.tokens:
            continue
        prose, actual = _rgb(m.group("hex")), _rgb(ds.tokens[token])
        if not prose or not actual or prose == actual:
            continue
        delta = contrast(prose, actual)
        # A near-identical shade is a rounding difference, not a contradiction.
        sev = "P0" if delta > 2.0 else "P2"
        out.append({"severity": sev, "rule": "prose-token-drift",
                    "message": f"DESIGN.md says {role} is {m.group('hex')} but "
                               f"{token} is {ds.tokens[token]} "
                               f"(contrast {delta:.2f}:1 apart)"})

    # --- 2. does the palette pass its own accessibility bar ---
    pairs = [("body text", "--fg", "--bg"), ("muted text", "--muted", "--bg"),
             ("accent text", "--accent-on", "--accent")]
    for label, fg_t, bg_t in pairs:
        fg, bg = ds.tokens.get(fg_t), ds.tokens.get(bg_t)
        if not fg or not bg:
            continue
        a, b = _rgb(fg), _rgb(bg)
        if not a or not b:
            continue
        r = contrast(a, b)
        if r < 3.0:
            out.append({"severity": "P0", "rule": "palette-contrast",
                        "message": f"{label}: {fg_t} on {bg_t} is {r:.2f}:1 "
                                   f"-- unusable at any size"})
        elif r < aa_normal:
            out.append({"severity": "P1", "rule": "palette-contrast",
                        "message": f"{label}: {fg_t} on {bg_t} is {r:.2f}:1 "
                                   f"(AA needs {aa_normal}:1 for normal text)"})

    # --- 3. an accent equal to the foreground is not an accent ---
    acc, fg = ds.tokens.get("--accent"), ds.tokens.get("--fg")
    if acc and fg and _rgb(acc) and _rgb(acc) == _rgb(fg):
        out.append({"severity": "P2", "rule": "no-accent",
                    "message": f"--accent equals --fg ({acc}); the system has no "
                               f"accent colour, so colour carries no hierarchy"})

    # --- 4. fonts that will not render as designed ---
    inst = installed_font_families()
    if inst:
        dead, degraded = [], []
        for stack in ds.font_stacks():
            named = stack["named"]
            if any(f in inst for f in named):
                if named[0] not in inst:
                    degraded.append(named[0])
            elif stack["generic"]:
                degraded.append(named[0])   # lands on the author's own fallback
            else:
                dead.append(named[0])       # no terminator: browser default
        if dead:
            out.append({"severity": "P1", "rule": "missing-fonts",
                        "message": f"{len(dead)} font stack(s) have no installed "
                                   f"family and no generic fallback, so they land "
                                   f"on the browser default: "
                                   f"{', '.join(sorted(set(dead))[:5])}"})
        if degraded:
            out.append({"severity": "P2", "rule": "degraded-fonts",
                        "message": f"{len(degraded)} stack(s) miss their preferred "
                                   f"family but resolve to a later one: "
                                   f"{', '.join(sorted(set(degraded))[:5])}"})

    # --- 5. completeness ---
    for req in ("--bg", "--fg", "--accent"):
        if req not in ds.tokens:
            out.append({"severity": "P0", "rule": "missing-token",
                        "message": f"required token {req} is not defined"})
    if not ds.design_md.strip():
        out.append({"severity": "P1", "rule": "no-design-md",
                    "message": "DESIGN.md is missing or empty; no prose guidance"})
    return out


def usable(ds_id: str) -> bool:
    """True when nothing P0 blocks selection."""
    return not any(f["severity"] == "P0" for f in validate(load(ds_id)))


def repair(ds: DesignSystem, write: bool = False) -> list[str]:
    """Reconcile DESIGN.md prose colour claims with tokens.css.

    45 of the 151 bundled packages assert colours in prose that contradict their
    own tokens -- `dashboard` describes a dark theme its tokens define as light.
    Both are injected into the prompt and both claim authority, so the model gets
    two palettes. tokens.css wins by the packages' own USAGE.md ("the DESIGN.md
    above is prose; this is the binding contract"), so the prose is what moves.

    Returns a list of human-readable changes; writes DESIGN.md only if write=True.
    """
    changes: list[str] = []
    text = ds.design_md
    if not text.strip():
        return changes

    def fix(m: re.Match) -> str:
        role = m.group("role").strip().lower()
        token = _ROLE_TOKEN.get(role)
        if not token or token not in ds.tokens:
            return m.group(0)
        prose, actual = _rgb(m.group("hex")), _rgb(ds.tokens[token])
        if not prose or not actual or prose == actual:
            return m.group(0)
        want = ds.tokens[token].strip()
        changes.append(f"{role}: {m.group('hex')} -> {want} (from {token})")
        return m.group(0).replace(m.group("hex"), want)

    new = _PROSE_COLOR.sub(fix, text)
    if changes and write:
        with open(os.path.join(ds.path, "DESIGN.md"), "w", encoding="utf-8") as fh:
            fh.write(new)
    return changes
