"""The design loop: task in, verified artifact out.

    generate -> render -> audit -> grade -> revise (bounded) -> keep the best

Design system and template are selected by id from config, so both are validated
before a run starts: a package whose prose contradicts its own tokens would hand
the model two different palettes, and failing at config time is much cheaper than
failing three iterations in.

The loop keeps the BEST-scoring artifact rather than the last one, because
revision is not monotonic -- iteration 3 can be worse than iteration 1.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil

from . import critique, designsystem, llm, opencode, render, template, verify

CONTEXT_DIR = ".designer"


@dataclasses.dataclass
class DesignTask:
    """One unit of work, matching the planner schema (kind: design)."""
    task_id: str
    title: str
    instructions: str = ""
    deliverable: str = "index.html"
    components: list[str] = dataclasses.field(default_factory=list)
    viewport: tuple[int, int] = (1440, 900)
    template: str = "web-prototype"
    design_system: str = "linear-app"

    @classmethod
    def from_dict(cls, d: dict) -> "DesignTask":
        acc = d.get("acceptance") or {}
        vp = acc.get("viewport") or d.get("viewport") or [1440, 900]
        return cls(
            task_id=d.get("task_id", "T000"), title=d.get("title", ""),
            instructions=d.get("instructions", ""),
            deliverable=d.get("deliverable", "index.html"),
            components=acc.get("components") or d.get("components") or [],
            viewport=(int(vp[0]), int(vp[1])),
            template=d.get("template", "web-prototype"),
            design_system=d.get("design_system", "linear-app"),
        )


# Context budget in BYTES, derived from the slot rather than guessed. Roughly
# 4 bytes per token; reserve half the slot for output and reasoning. At the
# current 32768-token slot this is ~48KB, enough for the full design system,
# template and layout library with nothing truncated. Raising the server's
# --ctx-size (or dropping --parallel to 1) widens this automatically via
# DESIGNER_SLOT_CONTEXT -- the model itself trains to 131072.
_SLOT = int(os.environ.get("DESIGNER_SLOT_CONTEXT", 32768))
CONTEXT_BUDGET_BYTES = int(os.environ.get("DESIGNER_CONTEXT_BUDGET", _SLOT * 4 * 0.45))


def _write_context(workdir: str, ds, tpl,
                   budget: int = CONTEXT_BUDGET_BYTES) -> list[str]:
    """Materialise design system + template as files to attach, within budget.

    Priority order matters. tokens.css is the binding contract and tiny; the seed
    replaces generation with composition; DESIGN.md is the largest and the least
    binding (USAGE.md itself says tokens.css wins), so it is trimmed first.
    """
    cdir = os.path.join(workdir, CONTEXT_DIR)
    os.makedirs(cdir, exist_ok=True)

    # seed.html is deliberately absent: it is pre-copied to the deliverable in
    # the working directory instead. Attaching it made the model edit the
    # attachment in place rather than produce an artifact.
    candidates = [("tokens.css", ds.tokens_css if ds else ""),
                  ("TEMPLATE.md", tpl.body),
                  ("layouts.md", tpl.layouts),
                  ("checklist.md", tpl.checklist),
                  ("DESIGN.md", ds.design_md if ds else "")]

    written, used = [], 0
    for name, body in candidates:
        if not body.strip():
            continue
        room = budget - used
        if room < 800:
            break
        if len(body) > room:
            # Keep the head: these documents lead with the rules that matter.
            body = body[:room] + "\n\n[...truncated to fit the context budget]\n"
        p = os.path.join(cdir, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        written.append(p)
        used += len(body)
    return written


def _prompt(task: DesignTask, ds, tpl) -> str:
    lines = [
        f"Design task {task.task_id}: {task.title}",
        "",
        task.instructions.strip(),
        "",
    ]
    if tpl.seed_html:
        lines += [
            f"'{task.deliverable}' ALREADY EXISTS in the working directory and "
            f"contains the design seed. Edit that file in place -- do not create "
            f"a new one and do not start from scratch. Replace every [REPLACE] "
            f"placeholder with real content, set the :root token values to match "
            f"tokens.css, and paste in the sections you need from layouts.md.",
        ]
    else:
        lines += [
            f"Produce a single self-contained HTML file named "
            f"'{task.deliverable}' in the working directory.",
        ]
    lines += [
        "Inline all CSS in one <style> block. No external stylesheets, scripts, "
        "fonts or images.",
        f"Design for a {task.viewport[0]}x{task.viewport[1]} viewport.",
    ]
    if task.components:
        lines.append("It must contain these components: "
                     + ", ".join(task.components) + ".")
    lines.append("")
    lines.append("Attached context:")
    lines.append("  TEMPLATE.md - the structure to follow. Follow it exactly.")
    if tpl.layouts:
        lines.append("  layouts.md  - paste-ready sections. Use these rather "
                     "than inventing markup.")
    if ds is not None:
        lines.append("  tokens.css  - the binding palette. Paste the :root block "
                     "verbatim into your <style>. Never write a raw hex outside "
                     "it.")
        lines.append("  DESIGN.md   - visual intent. Where it disagrees with "
                     "tokens.css, tokens.css wins.")
    if tpl.checklist:
        lines.append("  checklist.md - self-review before you finish.")
    lines.append("")
    lines.append("No placeholders, no TODOs, no lorem ipsum, no invented "
                 "statistics, no emoji as icons.")
    return "\n".join(lines)


def _direct_prompt(task: DesignTask, ds, tpl,
                   budget: int = CONTEXT_BUDGET_BYTES) -> str:
    """Everything the model needs in one prompt, for the direct engine.

    Sized to the budget in the same priority order as the attachment path:
    tokens.css binds, the template gives structure, layouts supply markup, and
    DESIGN.md prose is trimmed first because tokens.css already outranks it.
    """
    parts, used = [], 0

    def add(header: str, body: str):
        nonlocal used
        if not body.strip():
            return
        room = budget - used
        if room < 800:
            return
        if len(body) > room:
            body = body[:room] + "\n[...truncated]"
        parts.append(f"{header}\n{body}")
        used += len(body)

    if ds is not None:
        add("DESIGN TOKENS -- paste this :root block verbatim into your <style>, "
            "and never write a raw hex outside it:", ds.tokens_css)
    add("STRUCTURE TO FOLLOW:", tpl.body)
    add("SECTION LIBRARY -- paste from these rather than inventing markup:",
        tpl.layouts)
    if ds is not None:
        add("VISUAL INTENT (where this disagrees with the tokens above, the "
            "tokens win):", ds.design_md)

    req = [f"\nTASK: {task.title}"]
    if task.instructions.strip():
        req.append(task.instructions.strip())
    if task.components:
        req.append("The page must contain: " + ", ".join(task.components) + ".")
    req.append(
        f"Output ONE complete self-contained HTML document starting with "
        f"<!DOCTYPE html>, designed for a {task.viewport[0]}x{task.viewport[1]} "
        f"viewport. Inline all CSS in a single <style> block. No external "
        f"stylesheets, scripts, fonts or images.")
    req.append("No placeholders, no TODOs, no lorem ipsum, no invented "
               "statistics, no emoji as icons.")
    req.append("Output only the HTML document and nothing else.")
    return "\n\n".join(parts + req)


def _signature(workdir: str, task: DesignTask) -> str:
    """Content hash of the deliverable, to tell a real edit from a no-op turn."""
    p = os.path.join(workdir, task.deliverable)
    if not os.path.isfile(p):
        return ""
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _entry(workdir: str, task: DesignTask) -> str | None:
    """The artifact, preferring the requested name then any root-level HTML."""
    direct = os.path.join(workdir, task.deliverable)
    if os.path.isfile(direct):
        return direct
    cands = [f for f in os.listdir(workdir)
             if f.endswith((".html", ".htm"))
             and os.path.isfile(os.path.join(workdir, f))]
    if not cands:
        return None
    cands.sort(key=lambda f: (f != "index.html",
                              -os.path.getsize(os.path.join(workdir, f))))
    return os.path.join(workdir, cands[0])


def run_task(task: DesignTask, workdir: str, max_iters: int = 3,
             use_vision: bool = True, th: verify.Thresholds = verify.DEFAULTS,
             model: str = opencode.MODEL, engine: str = "direct") -> dict:
    """Generate, verify and revise one design task. Returns a report dict."""
    os.makedirs(workdir, exist_ok=True)

    tpl = template.load(task.template)
    tdef = template.validate(tpl)
    if any(f["severity"] == "P0" for f in tdef):
        return {"task_id": task.task_id, "outcome": "config_error",
                "error": f"template '{task.template}' is unusable: {tdef}"}

    ds = None
    if tpl.requires_design_system:
        ds = designsystem.load(task.design_system)
        dsdef = designsystem.validate(ds)
        blocking = [f for f in dsdef if f["severity"] == "P0"]
        if blocking:
            return {"task_id": task.task_id, "outcome": "config_error",
                    "error": f"design system '{task.design_system}' is unusable",
                    "defects": blocking}

    ctx_files = _write_context(workdir, ds, tpl)
    palette = ds.palette() if ds else []

    # Seed-based templates start from a complete, valid page the model edits.
    # This is the template's own documented workflow, and doing the copy here
    # removes the ambiguity that had the model mutating the context file.
    seeded = False
    if tpl.seed_html:
        dest = os.path.join(workdir, task.deliverable)
        if not os.path.exists(dest):
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(tpl.seed_html)
        seeded = True

    best = None
    session = None
    history = []
    prompt = _prompt(task, ds, tpl)

    for it in range(1, max_iters + 1):
        if engine == "direct":
            # Default engine. opencode's tool loop costs ~11k tokens of system
            # prompt and schemas before our content, and at that context length
            # gemma-4 stops emitting real tool calls -- it prints them as text
            # and halts. Generating the document as text and writing it here
            # removes tool-calling as a failure mode entirely.
            gen_prompt = prompt if it > 1 else _direct_prompt(task, ds, tpl)
            html, meta = llm.generate_html(gen_prompt)
            if html is None:
                history.append({"iteration": it, "outcome": "no_output",
                                "meta": meta})
                continue
            entry = os.path.join(workdir, task.deliverable)
            with open(entry, "w", encoding="utf-8") as fh:
                fh.write(html)
        else:
            before_sig = _signature(workdir, task)
            files, log, sid = opencode.run(
                prompt, workdir, files=None if session else ctx_files,
                session=session, model=model)
            session = session or sid
            entry = _entry(workdir, task)
            if entry is None or (seeded and _signature(workdir, task) == before_sig):
                why = "no_output" if entry is None else "unchanged"
                history.append({"iteration": it, "outcome": why})
                prompt = (f"You did not modify '{task.deliverable}'. Edit that "
                          f"file now: replace the [REPLACE] placeholders with "
                          f"real content for '{task.title}' and write it back.")
                continue

        with open(entry, encoding="utf-8", errors="replace") as fh:
            html = fh.read()
        audit = render.audit(entry, palette=palette,
                             components=task.components, viewport=task.viewport)
        findings = verify.tier1(html) + verify.tier2(audit, task.components, th=th)
        score = verify.score(findings)

        shot = os.path.join(workdir, f"{CONTEXT_DIR}/shot-{it}.png")
        vis = []
        if use_vision:
            try:
                render.screenshot(entry, shot, viewport=task.viewport)
                c = critique.critique(shot, intent=task.title,
                                      viewport=task.viewport)
                vis = critique.as_findings(
                    critique.keep_evidence_bearing(c["issues"]))
            except Exception as e:  # vision is advisory; never fail the run on it
                vis = [{"severity": "P2", "rule": "visual-error",
                        "message": f"critique unavailable: {type(e).__name__}"}]

        snap = {"iteration": it, "entry": os.path.relpath(entry, workdir),
                "score": score, "findings": findings, "visual": vis,
                "bytes": len(html)}
        history.append(snap)

        if best is None or score["penalty"] < best["score"]["penalty"]:
            keep = os.path.join(workdir, f"{CONTEXT_DIR}/best.html")
            shutil.copyfile(entry, keep)
            best = dict(snap, kept=keep)

        if score["passed"] and not vis:
            break
        if it == max_iters:
            break

        # Revision turn: same session, so the model does not re-read the design
        # system to fix one contrast ratio. Deterministic findings first.
        parts = [verify.render_for_agent(findings)]
        if vis:
            parts.append("A reviewer looking at a screenshot also noted:\n"
                         + "\n".join(f'- {v["message"]}' for v in vis))
        if engine == "direct":
            parts.append("Here is the current document:\n\n" + html)
            parts.append("Rewrite it fixing every defect above and changing "
                         "nothing else. Output only the complete HTML document.")
        else:
            parts.append(f"Rewrite '{os.path.basename(entry)}' fixing these. "
                         f"Change nothing else.")
        prompt = "\n\n".join(parts)

    if best is None:
        return {"task_id": task.task_id, "outcome": "no_output",
                "iterations": history}
    return {
        "task_id": task.task_id,
        "outcome": "designed" if best["score"]["passed"] else "defective",
        "best_iteration": best["iteration"],
        "artifact": best["kept"],
        "score": best["score"],
        "findings": best["findings"],
        "visual": best["visual"],
        "iterations": history,
        "design_system": task.design_system if ds else None,
        "template": task.template,
    }


def run_plan(tasks: list[dict], outdir: str, **kw) -> dict:
    """Run every kind=design task in a plan. Non-design tasks are skipped."""
    os.makedirs(outdir, exist_ok=True)
    reports = []
    skipped: dict = {}
    for raw in tasks:
        if raw.get("kind") not in (None, "design"):
            k = raw.get("kind") or "unknown"
            skipped[k] = skipped.get(k, 0) + 1
            continue
        task = DesignTask.from_dict(raw)
        wd = os.path.join(outdir, task.task_id)
        reports.append(run_task(task, wd, **kw))
    # Misclassification upstream is silent otherwise: a UI-heavy project whose
    # plan contains no design tasks looks identical to a plan with none needed.
    summary = {"total": len(reports), "skipped_by_kind": skipped,
               "designed": sum(1 for r in reports if r["outcome"] == "designed"),
               "defective": sum(1 for r in reports if r["outcome"] == "defective"),
               "failed": sum(1 for r in reports
                             if r["outcome"] in ("no_output", "config_error"))}
    out = {"summary": summary, "reports": reports}
    with open(os.path.join(outdir, "design_report.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    return out
