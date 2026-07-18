"""Load a design template: the structural shape of the artifact to produce.

A template is a SKILL.md (YAML front-matter + prose) plus optional side files --
a seed HTML the model composes rather than writing CSS from scratch, a library of
paste-ready section layouts, and a self-review checklist.

Templates come in two shapes, and the difference matters for a small local model:
  seed-based   (web-prototype, mobile-app) -- copy the seed, swap tokens, paste
               sections. Expensive in context (~9k) but replaces generation with
               composition, which is far more reliable.
  prompt-only  (dashboard, saas-landing, wireframe-*) -- prose region spec only
               (~1k), the model writes everything.
"""

from __future__ import annotations

import dataclasses
import os
import re

DATA_ROOT = os.environ.get(
    "DESIGNER_DATA",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data"))
TEMPLATES_DIR = os.path.abspath(os.path.join(DATA_ROOT, "templates"))

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v.strip("\"'")


def _mini_yaml(text: str) -> dict:
    """Enough YAML for these front-matter blocks: nested maps, block sequences,
    inline lists and `key: |` block scalars. Recursive-descent on indentation --
    avoids a pyyaml dependency for ~20 lines of highly regular metadata."""
    lines = text.split("\n")
    pos = 0

    def parse(indent: int):
        nonlocal pos
        result = None
        while pos < len(lines):
            raw = lines[pos]
            if not raw.strip() or raw.lstrip().startswith("#"):
                pos += 1
                continue
            cur = len(raw) - len(raw.lstrip())
            if cur < indent:
                break
            line = raw.strip()

            if line.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    break
                result.append(_scalar(line[2:]))
                pos += 1
                continue

            if result is None:
                result = {}
            if not isinstance(result, dict) or ":" not in line:
                break
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            pos += 1

            if val in ("|", ">"):
                block = []
                while pos < len(lines):
                    nxt = lines[pos]
                    if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= cur:
                        break
                    block.append(nxt.strip())
                    pos += 1
                result[key] = " ".join(x for x in block if x)
            elif val == "":
                sub = parse(cur + 1)
                result[key] = sub if sub is not None else {}
            else:
                result[key] = _scalar(val)
        return result

    out = parse(0)
    return out if isinstance(out, dict) else {}


@dataclasses.dataclass
class Template:
    id: str
    path: str
    meta: dict
    body: str
    seed_html: str = ""
    layouts: str = ""
    checklist: str = ""

    @property
    def seed_based(self) -> bool:
        return bool(self.seed_html)

    @property
    def entry_file(self) -> str:
        prev = (self.meta.get("od") or {}).get("preview") or {}
        return prev.get("entry") or "index.html"

    @property
    def requires_design_system(self) -> bool:
        ds = (self.meta.get("od") or {}).get("design_system") or {}
        return bool(ds.get("requires"))

    def context_tokens(self) -> int:
        return (len(self.body) + len(self.seed_html) + len(self.layouts)
                + len(self.checklist)) // 4


def available() -> list[str]:
    if not os.path.isdir(TEMPLATES_DIR):
        return []
    return sorted(d for d in os.listdir(TEMPLATES_DIR)
                  if os.path.isfile(os.path.join(TEMPLATES_DIR, d, "SKILL.md")))


def load(tpl_id: str) -> Template:
    path = os.path.join(TEMPLATES_DIR, tpl_id)
    skill = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill):
        raise FileNotFoundError(
            f"unknown template '{tpl_id}'; available: {', '.join(available())}")

    def rd(*parts: str) -> str:
        p = os.path.join(path, *parts)
        if not os.path.isfile(p):
            return ""
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    raw = rd("SKILL.md")
    m = _FM.match(raw)
    meta = _mini_yaml(m.group(1)) if m else {}
    body = raw[m.end():] if m else raw
    return Template(id=tpl_id, path=path, meta=meta, body=body,
                    seed_html=rd("assets", "template.html"),
                    layouts=rd("references", "layouts.md"),
                    checklist=rd("references", "checklist.md"))


def validate(t: Template) -> list[dict]:
    """Deterministic defects in the template itself."""
    out = []
    if not t.meta.get("name"):
        out.append({"severity": "P1", "rule": "no-name",
                    "message": "SKILL.md front-matter has no `name`"})
    if not t.body.strip():
        out.append({"severity": "P0", "rule": "empty-body",
                    "message": "SKILL.md has no instruction body"})
    # A seed that mentions layouts but ships none leaves the model composing
    # from parts that do not exist.
    if t.seed_html and not t.layouts:
        out.append({"severity": "P1", "rule": "seed-without-layouts",
                    "message": "ships a seed but no references/layouts.md, so "
                               "there are no sections to compose from"})
    if t.layouts and not t.seed_html:
        out.append({"severity": "P2", "rule": "layouts-without-seed",
                    "message": "ships layouts but no seed; classes they use may "
                               "be undefined"})
    return out
