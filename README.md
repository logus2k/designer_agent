# Designer Agent

**Designer Agent takes design tasks and produces application frontends — with a local
model — then proves the result actually renders correctly instead of asking the model
whether it did a good job.**

It is the design stage of a local, requirements-to-code pipeline. [Planner Agent](../planner_agent)
produces a plan of tasks; Designer Agent picks up the ones marked `kind: design`, generates a
self-contained HTML artifact with a **local model (Gemma 4 via llama.cpp)**, and verifies it by
**rendering it in a real browser and measuring the result**. Nothing leaves your machine.

```
reqoach  ──►  planner_agent  ──►  Designer Agent  ──►  builder_agent
requirements   plan of tasks       design tasks         code tasks
+ gaps         (design | code)     → verified HTML      → working files
```

---

## What it does

Given a design task, Designer Agent:

1. **Validates the configuration before spending a single token.** The design system and
   template are selected by id; both are checked first, because a package whose prose
   contradicts its own tokens will hand the model two different palettes.
2. **Generates a self-contained HTML artifact** with a local model, using a design system for
   the visual language and a template for the structure.
3. **Renders it in headless Chromium and measures it** — overflow, overlapping text, contrast
   ratios, palette adherence, edge gutters, component presence.
4. **Feeds the measurements back and revises**, bounded, keeping the *best-scoring* artifact
   rather than the last one.
5. **Reports honestly** — `designed` (verified clean), `defective` (produced but measurably
   broken, with the defects listed), or `no_output`.

---

## Why the verification matters

A model asked "does this look right?" says yes. Worse, a model asked to *find* problems invents
them: prompted as a harsh reviewer with a quota of three issues, this one returned a P0 about a
clean page being "generic… like a template from 2018" — after its own reasoning trace had
described the same page as well-structured.

So judgement is layered, and **only measurements gate**:

| Tier | What it checks | Gates? |
|---|---|---|
| **1. Structural** | file produced, HTML parses, no stub fingerprints (TODO, lorem ipsum, `[REPLACE]`) | yes |
| **2. Geometry** | **surface collisions**, viewport & content overflow, contrast ratios, off-palette colours, edge gutters, missing components, tap targets | yes |
| **3. Visual** | a vision model looks at a screenshot and names concrete defects | **never** |

Tier 2 runs in-page inside the rendered document and returns numbers, so a finding is an
instruction the model can act on — `h1 content spills outside its box: needs 3286px but the box
is 1376px` — not an opinion.

Tier 3 earns its place by catching what geometry cannot (a genuinely ugly colour relationship),
but its verdict field carries no information, so it only ever contributes revision hints. Issues
that assert taste rather than naming a visible element are filtered out before they reach the
model.

### Measured against human judgement

The verifier has been checked against a human ranking 14 generated artifacts blind — the only
ground truth in the project, and it reset several assumptions.

**`passed` is not a quality claim.** Of 8 artifacts scoring penalty 0, the reviewer accepted 4.
One scored a perfect 0 and was ranked worst of all. `passed: true` means *no detected breakage*,
which is a weaker statement than it sounds.

*(Those figures describe the verifier **as reviewed**. Surface-collision detection was added in
response, and the artifact ranked worst now scores 300 rather than 0 — so the headline number
overstates today's false-negative rate. Re-deriving it honestly needs a fresh blind review
against the current build; until then, treat 4-of-8 as the pre-fix baseline.)*

**Surface collisions are the strongest predictor.** Elements that *paint* — a card drawn over a
card, a sidebar bleeding across a header — flagged the reviewer's entire bottom tier with zero
false positives (TP 3, FP 0, FN 0). This was missed for most of development because overlap
detection compared only text-bearing elements; the colliding things are containers, which have
no direct text node.

**Vision cannot substitute for geometry here.** A purpose-written occlusion prompt scored **0/3
recall** on confirmed collisions. The reason is structural: occlusion destroys its own evidence.
The DOM knows two boxes intersect; the pixels only show the winner. Vision's value is elsewhere
— it catches missing axis labels and undersized icons, which geometry can never see.

Also validated against 106 real design artifacts: 0 probe errors. Iterating on that corpus
removed ~98% of false-positive overlap findings by learning to recognise deliberate stacking
(deck slides, carousels, marquees) — but that same tuning is what created the collision blind
spot, so filters are now only trusted when the human-labelled set confirms them.

Design decisions and the failures behind them are in
[`documents/technical_architecture.md`](documents/technical_architecture.md).

---

## Quickstart

You need a local llama.cpp server with a vision-capable model, and Chromium (the container
supplies its own).

```bash
export PYTHONPATH=src

# check the installation: renderer, fonts, design systems, templates
python3 -m designer.cli check -v

# grade an existing HTML file
python3 -m designer.cli audit page.html --design-system linear-app \
        --component header --component h1

# run every kind=design task in a plan
python3 -m designer.cli run-plan plan.json --out data/runs
```

Or in Docker, where Chromium and the fonts are baked in:

```bash
docker compose build
docker compose run --rm designer-agent check
docker compose run --rm designer-agent run-plan /app/data/tasks/sample_tasks.json
```

### A design task

```json
{
  "task_id": "D001",
  "kind": "design",
  "surface": "frontend",
  "title": "Landing page for a focus timer app",
  "instructions": "Hero, three feature cards, closing call to action.",
  "deliverable": "index.html",
  "template": "web-prototype",
  "design_system": "linear-app",
  "acceptance": {
    "kind": "design+verify",
    "components": ["header", "h1", "button"],
    "viewport": [1440, 900]
  }
}
```

`kind` routes the task (Designer takes `design`, [Builder](../builder_agent) takes `code`);
`components` is what makes acceptance machine-checkable.

---

## Design systems and templates

Bundled from [open-design](https://github.com/nexu-io/open-design) (Apache-2.0): **151 design
systems** and 6 web templates, living in `data/` so they can be added or corrected without
rebuilding the image.

A **design system** is the visual language — `tokens.css` (the binding palette), `DESIGN.md`
(prose intent). A **template** is the structural shape; the seed-based ones ship a complete
starting page plus a library of paste-ready sections, which lets a small model *compose* instead
of authoring CSS from scratch.

Because any of them can be selected from config, they are validated like any other input:

```
usable: 134/151
  12  palette-contrast   accent text unreadable on its own accent colour
   6  missing-token      no --bg defined
```

`designer check -v` lists the rest. 33 packages shipped prose that contradicted their own
tokens — one described a dark theme its tokens defined as light — and are reconciled on import
in favour of `tokens.css`, which the packages' own USAGE.md designates as binding. Systems with
genuine palette defects stay gated rather than being silently "fixed", since repairing those
would mean inventing design.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DESIGNER_SLOT_CONTEXT` | `32768` | Per-slot context of the model server. Prompt and attachment budgets derive from this — raise it when you raise the server's, or the pipeline keeps rationing context it already has. |
| `DESIGNER_MAX_TOKENS` | `16384` | Output budget. Sized to the **artifact**, not the slot: scaling it with context let reasoning run away without ever closing the document. |
| `DESIGNER_DATA` | `./data` | Design systems, templates, tasks, runs. |
| `DESIGNER_CHROME` | auto-detected | Browser binary. |
| `DESIGNER_MODEL` | `local-llama/gemma-4` | |

Grading policy lives in one place — `verify.Thresholds` — so severity and cut-offs can be retuned
without re-rendering anything.

---

## What it does not do yet

Stated plainly, because the gap between "runs" and "reliable" is where this kind of tool usually
oversells itself:

- **Convergence depends on the task.** With the full checks active, a landing-page task converged
  3/3 (`[20,0]`, `[20,0]`, `[0]`); a dashboard task converged 1/3. Revision sometimes no-ops
  entirely (`[531,531,531]`) and occasionally regresses (`[20,0,70]`) — which is why the
  best-scoring artifact is kept rather than the last.
- **Detection is not remediation.** In one run the model was handed four P0 surface-collision
  findings at the top of its revision prompt, twice, and fixed neither — it fixed a lower-priority
  contrast issue instead and the collision count stayed at 4, 4, 4. A colour fix is local; a
  collision fix means reasoning about the grid that caused it. Findings are also currently
  emitted per element pair, so one structural fault can appear four times and crowd the message.
- **One artifact at a time.** A real frontend is several screens sharing navigation and tokens;
  cross-artifact consistency is not checked.
- **No planner integration yet.** Tasks are currently hand-written; `surface` and `kind` are not
  yet emitted upstream.
- **Contrast dominates the findings** on real-world pages, and even professionally authored
  templates mostly fail WCAG AA. Whether the gate should be stricter than its own reference
  material is a product decision, which is why it is configurable rather than fixed.

---

## Repository layout

```
src/designer/
  design.py         the loop: generate -> render -> grade -> revise -> keep best
  llm.py            direct generation against the local model
  opencode.py       alternative engine driving opencode's tool loop
  render.py         headless Chromium: geometry audit + screenshots
  verify.py         tiers 1-2, grading policy, findings formatted for the model
  critique.py       tier 3 visual critique, evidence-filtered, non-gating
  designsystem.py   load / validate / repair design systems
  template.py       load / validate templates
  cli.py            command line entry point
  assets/audit.js   the in-page measurement probe
data/               design systems, templates, corpus, tasks, runs (bind-mounted)
fixtures/           known-good and deliberately-broken pages the checks are tested against
```

The audit probe is injected into a copy of the artifact and returns its measurements through
`--dump-dom` — no CDP client, no Playwright dependency, just the browser binary.

---

## Where it fits

| Stage | Project | Role |
|---|---|---|
| Requirements | [reqoach](../../labs/requirements) | score requirements, find gaps |
| Architecture | [architect_agent](../architect_agent) | system decomposition, frontend/backend boundary |
| Plan | [planner_agent](../planner_agent) | break requirements into buildable tasks |
| **Design** | **Designer Agent** (this repo) | **design each frontend task and verify it renders** |
| Build | [builder_agent](../builder_agent) | build each code task locally and verify it |

Designer Agent only designs what the plan marked `kind: design` — it does not decide *what* to
build.

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).

Bundled design systems and templates are derived from
[open-design](https://github.com/nexu-io/open-design) (Apache-2.0). Bundled fonts are Inter and
JetBrains Mono, both SIL OFL 1.1 — see [`fonts/README.md`](fonts/README.md).
