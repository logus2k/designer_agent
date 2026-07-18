# Designer Agent — Technical Architecture

How the pieces fit, and — more usefully — **why**, since most of the non-obvious
decisions here were forced by measurement rather than chosen up front. Where a
design exists because an obvious alternative was tried and failed, that is
recorded, because the failure is the justification.

---

## 1. Pipeline position

```
reqoach ──► planner_agent ──► Designer Agent ──► builder_agent
requirements   plan of tasks    kind: design       kind: code
+ gaps         surface/kind     → verified HTML    → working files
```

Designer Agent owns one stage: turn a `kind: design` task into a single
self-contained HTML artifact and prove it renders correctly. It does not decide
*what* to build, and it does not produce production components — that handoff
belongs to builder_agent.

**Task routing is upstream by design.** If Designer decided which tasks were its
own, Builder would have to as well, and the two could disagree — tasks claimed
twice or dropped silently. `surface` (frontend/backend/shared/infra) is an
architectural boundary and belongs to architect_agent; `kind` (design/code) and
`template` are per-task and belong to planner_agent.

---

## 2. Module map

| Module | Responsibility |
|---|---|
| `design.py` | the loop: generate → render → grade → revise → keep best |
| `llm.py` | direct generation against the local llama.cpp server |
| `opencode.py` | alternative engine driving opencode's tool loop |
| `render.py` | headless Chromium: geometry audit + screenshots |
| `assets/audit.js` | the in-page measurement probe |
| `verify.py` | tiers 1–2, grading policy, findings formatted for the model |
| `critique.py` | tier 3 visual critique, evidence-filtered |
| `designsystem.py` | load / validate / repair design systems |
| `template.py` | load / validate templates |
| `cli.py` | command line entry point |

---

## 3. Generation: why not the tool loop

`opencode.py` exists and works — it drives opencode's agent loop, writes files,
and continues sessions. It is **not** the default engine.

opencode's system prompt plus tool schemas cost roughly 11k tokens before any of
our content. With a design system and template attached, input reached **18,383
tokens** against a 32,768-token slot, and gemma-4 stopped emitting real tool
calls: it printed the call as plain text (`[read{filePath: "..."}]`) and halted
without writing anything. The same model writes files correctly through opencode
when the prompt is short — tool-calling reliability degrades with context length.

So `llm.py` skips the tool loop: put the context in the prompt, have the model
emit one complete HTML document as text, and write the file here. This is the
shape open-design calls the *text-artifact execution profile*. It removes
tool-calling as a failure mode and frees ~11k tokens.

**Budgets derive from the slot, with one deliberate asymmetry.** Input scales
with `DESIGNER_SLOT_CONTEXT`; output does not. Scaling the output cap with the
slot actively hurt — at 32,768 a revision turn burned 33k completion tokens on
reasoning and never closed the document. A page is 3–5k tokens; 16,384 is
generous. Input tracks the slot, output tracks the artifact.

---

## 4. Verification: three tiers, and what each is *for*

The organising principle: **only measurements gate.** A model asked "is this
good?" says yes; a model asked to find three problems invents three problems.

| Tier | Checks | Gates |
|---|---|---|
| 1 structural | produced, parses, no stub fingerprints | yes |
| 2 geometry | surface collisions, overflow, contrast, palette, gutters, components | yes |
| 3 visual | vision model names concrete defects from a screenshot | no |

### 4.1 The probe

`audit.js` is injected into a copy of the artifact, runs in page context, and
returns JSON through a `<script>` tag that `chrome --headless --dump-dom` carries
back out. No CDP client, no Playwright — just the browser binary.

Two implementation notes that cost real debugging time:

- An HTML parser ends a `<script>` at the first `</script` **regardless of JS
  context** — even inside a comment. The probe's own comment about that hazard
  silently truncated it. Both payloads are now escaped at injection.
- The probe **always emits a tag**, carrying `probe_error` if it throws. A probe
  that dies silently is indistinguishable from a clean page, which converts a
  crash into a false pass.

### 4.2 Surface collisions — the strongest signal

Two elements that *paint* (background, border, shadow) intersecting is a visual
collision: a card over a card, a sidebar bleeding across a header. This is the
single best predictor of a human rejecting a page.

It was missed for most of development because overlap detection compared only
**text-bearing** elements. The colliding things are containers: a card has no
direct text node, an input has none, an avatar circle has none.

**Vision cannot substitute for this.** Tested directly: a purpose-written
occlusion prompt scored **0/3 recall** on confirmed collisions. The reason is
structural — *occlusion destroys its own evidence*. The DOM knows two boxes
intersect; the pixels only show the winner. On one artifact a sidebar covered the
search field and avatar entirely, so there was nothing in the image to see.
Partial overlap leaving a visible seam is detectable by vision; full occlusion
never is.

Three exclusions, each for an idiom rather than to move a number:

- **stacked panes** — sibling containers occupying the same box (deck slides,
  carousels, tab panels). Compares against the *larger* box: near-total mutual
  coverage is stacking; a badge on a hero covers only a sliver.
- **backdrops** — a layer covering ≥85% of the viewport is a background.
- **same-kind siblings** — overlapping avatar rows and fanned cards. Matched by
  *shared class token*, not identical class strings: the idiom is a base class
  plus a modifier (`av av-cale`, `av a-dp`).

### 4.3 Other tier-2 checks

- **Overflow** is two distinct failures. A box can sit past the viewport edge, or
  sit inside it while its *content* spills out. The second is invisible to a
  bounding-rect test — `rect.right` stays inside — and needs
  `scrollWidth` vs `clientWidth`.
- **Contrast** measures every text element and lets `verify.py` grade, so
  thresholds retune without re-rendering. Text over a gradient is scored against
  every stop, worst case kept.
- **Edge gutter** catches text flush against the viewport, which neither
  overflows nor overlaps.
- **Scroller-aware**: content wider than its parent is intentional inside
  `overflow-x: auto|scroll|hidden`. `body`/`html` are excluded — a page-level
  `overflow-x: hidden` is a common reset, not a component.

### 4.4 Tier 3 and why it never gates

Prompted as a harsh reviewer with a quota of three issues, the model returned a
P0 about a clean page being "generic… like a template from 2018" — after its own
reasoning had described that page as well-structured. Removing the quota and
allowing an empty list fixed the false positives.

Its verdict field carries no information, so it only contributes revision hints,
filtered to issues naming a visible element. Its real value is **content
semantics** — missing axis labels, undersized icons — which geometry can never
see. Geometry for layout integrity, vision for content sense.

---

## 5. Configuration is validated, not trusted

Any of ~151 design systems can be selected by id, so they are validated like any
other input: prose/token agreement, palette contrast, token completeness, font
availability. 45 packages shipped prose contradicting their own tokens — one
described a dark theme its tokens defined as light — and both are injected into
the prompt, both claiming authority.

Contradictions are **reconciled on import** in favour of `tokens.css` (which the
packages' own USAGE.md designates binding). Genuine palette defects are **gated,
not repaired** — fixing those would mean inventing design.

Validation runs before generation, because failing at config time is far cheaper
than failing three iterations in.

---

## 6. The loop

```
validate config → generate → render → grade → revise (bounded) → keep best
```

**Best, not last.** Revision is not monotonic. Observed trajectories include
`[20, 0, 70]` (reached 0, then regressed), `[531, 531, 531]` (three iterations,
no measurable change) and `[1940, 200, 30]` (steady progress, ran out of
iterations). Keeping the last artifact would have shipped the worse one in
several runs.

---

## 7. Known limits

- **`passed` means "no detected breakage", not "good."** Against human ranking of
  14 artifacts, the full penalty scores ρ ≈ 0.57. Surface collisions classify the
  worst tier perfectly (TP 3, FP 0, FN 0), but nothing yet distinguishes
  *acceptable* from *mediocre-but-intact* — six artifacts with zero collisions
  were still rejected by a human reviewer.
- **One artifact at a time.** No cross-artifact consistency checking.
- **Semantics are unmeasured.** An inverted chart axis is geometrically perfect.
- **Missing glyphs are invisible.** A tofu box is a text node with fine contrast,
  which is why fonts are baked into the image rather than assumed.
- **Corpus false positives are a known, named class.** 9 of 50 non-deck corpus
  files report a collision. Inspecting the renders (not the class names) confirms
  the top cases are all deliberate overlays: a floating settings drawer with a
  close button (`tweaks`), a rotated pullquote card laid across a numbered list
  (`digital-eguide`), an annotation float pinned over a KPI (`wireframe-greybox`).
  Rotated elements are a systematic contributor -- a CSS transform inflates the
  bounding box well beyond the visual footprint.

  These are **not** filtered out. Doing so would need exclusions validated
  against more than three positive examples, and over-tuning against this
  unlabelled corpus is exactly how overlap detection was previously suppressed
  into missing real collisions. The human-labelled set remains the only ground
  truth, and every filter change is checked against it.
