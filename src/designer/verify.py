"""Tier 1 + tier 2b verification: turn a geometry audit into graded findings.

Deterministic only. Every finding here is a measurement with a number attached,
so it can be handed back to the model as a concrete instruction ("this element
overflows by 214px") rather than an opinion. Aesthetic judgement belongs to the
vision tier and never gates.

Severities follow open-design's convention so ported anti-slop lint findings and
these can share one report:
  P0  ships broken   -- gates
  P1  clearly wrong  -- gates
  P2  worth fixing   -- reported, does not gate
"""

from __future__ import annotations

import dataclasses
import html.parser
import re

GATING = ("P0", "P1")


@dataclasses.dataclass
class Thresholds:
    """Grading policy. The probe measures; this decides what counts as a defect.

    Deliberately all in one place and all overridable: the right values are an
    empirical question, and re-tuning must not require re-rendering anything.
    """

    # --- contrast (WCAG 2.1 ratios) ---
    contrast_aa_normal: float = 4.5      # AA floor, text < 24px (or <18.66px bold)
    contrast_aa_large: float = 3.0       # AA floor, large text
    contrast_hard_floor: float = 3.0     # below this -> P0 regardless of size
    contrast_marginal_severity: str = "P1"   # AA-fail but above hard floor
    # set to "P2" to report-without-gating marginal contrast

    # --- geometry ---
    overflow_p0_px: float = 24.0         # spill beyond this -> P0, else P1
    # own overflow-x:hidden + spilling content is usually deliberate clipping
    content_clipped_severity: str = "P2"
    overlap_p0_fraction: float = 0.35    # fraction of smaller element covered
    # painted surfaces intersecting -- validated against human review as the
    # single strongest predictor of a page being rejected outright
    surface_collision_min: float = 0.02
    surface_collision_severity: str = "P0"
    overlap_min_fraction: float = 0.10   # below this, not reported at all

    # --- palette / targets ---
    off_palette_p1_count: int = 3        # more distinct off-token colors -> P1
    off_palette_severity_low: str = "P2"
    edge_gutter_min_px: int = 8       # text closer than this to a viewport edge
    edge_gutter_severity: str = "P1"
    tap_target_min_px: int = 24
    tap_target_severity: str = "P2"
    tap_target_skip_inline: bool = True

    def contrast_required(self, large: bool) -> float:
        return self.contrast_aa_large if large else self.contrast_aa_normal


DEFAULTS = Thresholds()


class _Parse(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.ok = True
        self.stack = []
        self.void = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                     "link", "meta", "param", "source", "track", "wbr"}

    def handle_starttag(self, tag, attrs):
        if tag not in self.void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.void:
            return
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            self.ok = False


STUB_PATTERNS = [
    (r"\bTODO\b", "TODO marker"),
    (r"\bFIXME\b", "FIXME marker"),
    (r"lorem\s+ipsum", "lorem ipsum filler"),
    (r"\bplaceholder\s+(text|content)\b", "placeholder copy"),
    (r"feature\s+(one|two|three)\b", "unfilled feature slots"),
    (r"\byour\s+(title|heading|text)\s+here\b", "template copy"),
    (r"\[REPLACE\]", "unreplaced template token"),
]


def tier1(html_text: str) -> list[dict]:
    """Produced / parses / not-a-stub. Ported from builder_agent's discipline."""
    out = []
    if not html_text.strip():
        return [{"severity": "P0", "rule": "empty", "message": "artifact is empty"}]

    p = _Parse()
    try:
        p.feed(html_text)
    except Exception as e:
        out.append({"severity": "P0", "rule": "parse",
                    "message": f"HTML failed to parse: {e}"})
    else:
        if not p.ok or p.stack:
            out.append({"severity": "P0", "rule": "parse",
                        "message": f"unbalanced tags; unclosed: {p.stack[:5]}"})

    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text,
                  flags=re.S | re.I)
    for pat, label in STUB_PATTERNS:
        if re.search(pat, body, re.I):
            out.append({"severity": "P0", "rule": "stub",
                        "message": f"stub fingerprint: {label}"})
    return out


def tier2(audit: dict, required_components: list[str] | None = None,
          th: Thresholds = DEFAULTS) -> list[dict]:
    """Geometry, contrast, palette and component checks from a rendered audit."""
    out = []
    vw = audit.get("viewport", [0, 0])[0]

    if audit.get("doc_overflow_px", 0) > 0:
        out.append({"severity": "P0", "rule": "page-overflow",
                    "message": f"page scrolls horizontally: content is "
                               f"{audit['doc_overflow_px']}px wider than the "
                               f"{vw}px viewport"})

    for o in audit.get("overflow", []):
        sev = "P0" if o["overflow_px"] > th.overflow_p0_px else "P1"
        txt = f' -- text starts "{o["text"]}"' if o.get("text") else ""
        out.append({"severity": sev, "rule": "element-overflow",
                    "path": o["path"],
                    "message": f'{o["path"]} extends {o["overflow_px"]}px past '
                               f'the {o["side"]} viewport edge{txt}'})

    for o in audit.get("content_overflow", []):
        if o["clipped"]:
            sev = th.content_clipped_severity
        else:
            sev = "P0" if o["spill_px"] > th.overflow_p0_px else "P1"
        what = "is clipped and unreadable" if o["clipped"] else "spills outside its box"
        txt = f' -- text starts "{o["text"]}"' if o.get("text") else ""
        out.append({"severity": sev, "rule": "content-overflow", "path": o["path"],
                    "message": f'{o["path"]} content {what}: needs '
                               f'{o["scroll_width"]}px but the box is '
                               f'{o["client_width"]}px ({o["spill_px"]}px over){txt}'})

    for o in audit.get("overlaps", []):
        if o["fraction_of_smaller"] < th.overlap_min_fraction:
            continue
        sev = "P0" if o["fraction_of_smaller"] > th.overlap_p0_fraction else "P1"
        out.append({"severity": sev, "rule": "overlap", "path": o["a"],
                    "message": f'{o["a"]} overlaps {o["b"]} '
                               f'({int(o["fraction_of_smaller"] * 100)}% of the '
                               f'smaller element is covered)'})

    # One structural fault (a grid overflowing the panel below it) surfaces as
    # many element pairs. A measured run showed four identical findings crowding
    # the revision prompt while the model fixed none of them -- so collapse by
    # the pair of *kinds* involved and describe the fix geometrically.
    groups: dict = {}
    for c in audit.get("surface_collisions", []):
        if c["fraction"] < th.surface_collision_min:
            continue
        key = (c["a"].split(">")[-1], c["b"].split(">")[-1])
        g = groups.setdefault(key, {"n": 0, "worst": c})
        g["n"] += 1
        if c["fraction"] > g["worst"]["fraction"]:
            g["worst"] = c
    for (ka, kb), g in groups.items():
        c = g["worst"]
        ar, br = c.get("a_rect"), c.get("b_rect")
        where = ""
        if ar and br:
            dx, dy = c.get("dx", 0), c.get("dy", 0)
            # Say which direction to move, in the units the model wrote the CSS in.
            if dy and dx and dy <= dx:
                dirn = ("upward into" if ar[1] < br[1] else "downward into")
                where = (f"; it extends {dy}px {dirn} the other element's row "
                         f"-- give the container {dy}px more height or move the "
                         f"following block down")
            elif dx:
                dirn = ("leftward into" if ar[0] < br[0] else "rightward into")
                where = (f"; it extends {dx}px {dirn} the other element's column "
                         f"-- reduce its width or the number of items per row")
            where += (f" [{ka} at x={ar[0]},y={ar[1]} {ar[2]}x{ar[3]}; "
                      f"{kb} at x={br[0]},y={br[1]} {br[2]}x{br[3]}]")
        count = f" ({g['n']} element pairs affected)" if g["n"] > 1 else ""
        out.append({"severity": th.surface_collision_severity,
                    "rule": "surface-collision", "path": c["a"],
                    "message": f'{ka} is drawn over {kb}'
                               f'{count} -- {int(c["fraction"] * 100)}% of the '
                               f'smaller element is covered{where}'})

    for e in audit.get("edge_touching", []):
        if e["gap_px"] >= th.edge_gutter_min_px:
            continue
        out.append({"severity": th.edge_gutter_severity, "rule": "edge-gutter",
                    "path": e["path"],
                    "message": f'{e["path"]} sits {e["gap_px"]}px from the '
                               f'{e["side"]} viewport edge (needs at least '
                               f'{th.edge_gutter_min_px}px) -- "{e["text"]}"'})

    for c in audit.get("text_contrast", []):
        need = th.contrast_required(c["large"])
        if c["ratio"] >= need:
            continue
        sev = "P0" if c["ratio"] < th.contrast_hard_floor else th.contrast_marginal_severity
        out.append({"severity": sev, "rule": "contrast", "path": c["path"],
                    "ratio": c["ratio"],
                    "message": f'{c["path"]} contrast {c["ratio"]}:1 '
                               f'(needs {need}:1) -- {c["fg"]} on '
                               f'{c["bg"]} at {c["font_px"]}px'})

    off = audit.get("off_palette", [])
    if off:
        top = ", ".join(f'{o["color"]}x{o["count"]}' for o in off[:6])
        sev = "P1" if len(off) > th.off_palette_p1_count else th.off_palette_severity_low
        out.append({"severity": sev, "rule": "off-palette",
                    "message": f"{len(off)} colors used that are not design-system "
                               f"tokens: {top}"})

    comps = audit.get("components", {})
    for sel in (required_components or []):
        if comps.get(sel, 0) == 0:
            out.append({"severity": "P0", "rule": "missing-component",
                        "message": f"required component '{sel}' not present in "
                                   f"the rendered page"})

    for t in audit.get("targets", []):
        if th.tap_target_skip_inline and t.get("inline"):
            continue
        w, h = t["size"]
        if w >= th.tap_target_min_px and h >= th.tap_target_min_px:
            continue
        out.append({"severity": th.tap_target_severity, "rule": "tap-target",
                    "path": t["path"],
                    "message": f'{t["path"]} is {w}x{h}px (minimum '
                               f'{th.tap_target_min_px}x{th.tap_target_min_px})'})
    return out


def score(findings: list[dict]) -> dict:
    """Collapse findings into a gate decision and a comparable score."""
    counts = {"P0": 0, "P1": 0, "P2": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "passed": counts["P0"] == 0 and counts["P1"] == 0,
        "counts": counts,
        # lower is better; used to keep the best revision, not the last
        "penalty": counts["P0"] * 100 + counts["P1"] * 10 + counts["P2"],
    }


def render_for_agent(findings: list[dict], limit: int = 12) -> str:
    """Format findings as a revision instruction for the model."""
    gating = [f for f in findings if f["severity"] in GATING]
    rest = [f for f in findings if f["severity"] not in GATING]
    lines = []
    for f in (gating + rest)[:limit]:
        lines.append(f'[{f["severity"]}] {f["rule"]}: {f["message"]}')
    if not lines:
        return "No deterministic defects found."
    return ("The rendered page has these measured defects. Fix each one:\n"
            + "\n".join(lines))
