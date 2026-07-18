/* Layout/geometry probe. Injected into the artifact, runs in page context after
 * load, and writes its findings into a <script id="__od_audit__"> tag so a plain
 * `chrome --headless --dump-dom` run can carry them back out. No CDP, no deps.
 *
 * Measures only what code can measure honestly: geometry, contrast, palette
 * membership, component presence. Aesthetic judgement is not this file's job. */
(() => {
function collect() {
  const CFG = window.__OD_AUDIT__ || {};
  const VW = window.innerWidth;
  const TOL = 1; // px slack, sub-pixel rounding

  /* ---------- color ---------- */
  function parseColor(s) {
    if (!s) return null;
    s = String(s).trim();
    let m = s.match(/^rgba?\(([^)]+)\)/i);
    if (m) {
      const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
      if (p.length >= 3 && p.slice(0, 3).every(n => !isNaN(n)))
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    m = s.match(/^#([0-9a-f]{3,8})$/i);
    if (m) {
      let h = m[1];
      if (h.length === 3 || h.length === 4) h = h.split('').map(c => c + c).join('');
      if (h.length === 6 || h.length === 8)
        return {
          r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16),
          b: parseInt(h.slice(4, 6), 16), a: h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1,
        };
    }
    return null;
  }
  const hex = c => '#' + [c.r, c.g, c.b].map(v => Math.round(v).toString(16).padStart(2, '0')).join('');
  function luminance(c) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function contrast(a, b) {
    const l1 = luminance(a), l2 = luminance(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }

  /* A gradient has no single backdrop color, so score text against every stop and
   * keep the worst. Beats reporting "indeterminate" and missing a real failure. */
  function gradientStops(bgImage) {
    if (!bgImage || bgImage === 'none') return [];
    const out = [];
    const re = /(rgba?\([^)]+\)|#[0-9a-f]{3,8})/gi;
    let m;
    while ((m = re.exec(bgImage))) { const c = parseColor(m[1]); if (c) out.push(c); }
    return out;
  }

  function backdrops(el) {
    const out = [];
    let n = el;
    while (n && n.nodeType === 1) {
      const cs = getComputedStyle(n);
      const stops = gradientStops(cs.backgroundImage);
      if (stops.length) { out.push(...stops); return out; }
      const bc = parseColor(cs.backgroundColor);
      if (bc && bc.a > 0.05) { out.push(bc); if (bc.a > 0.95) return out; }
      n = n.parentElement;
    }
    if (!out.length) out.push({ r: 255, g: 255, b: 255, a: 1 });
    return out;
  }

  /* ---------- element selection ---------- */
  function pathOf(el) {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 5) {
      let s = n.tagName.toLowerCase();
      if (n.id) { parts.unshift(s + '#' + n.id); break; }
      const cls = (n.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean)[0];
      if (cls) s += '.' + cls;
      const od = n.getAttribute && n.getAttribute('data-od-id');
      if (od) s += `[data-od-id="${od}"]`;
      parts.unshift(s);
      n = n.parentElement;
    }
    return parts.join('>');
  }
  const visible = (el, r) => {
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.display !== 'none' &&
      cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0.05;
  };
  const hasDirectText = el => {
    for (const n of el.childNodes)
      if (n.nodeType === 3 && n.textContent.trim()) return true;
    return false;
  };

  /* Horizontally on-canvas. Content parked entirely left/right of the viewport
   * is normal for slide decks and carousels (stacked panes translated out of
   * view), and treating it as broken layout produced ~70 bogus overlap findings
   * per file across the open-design corpus. Vertical position is NOT filtered:
   * below-the-fold is legitimate page content. */
  const onCanvas = r => r.right > 0 && r.left < VW;

  /* Content wider than its parent is intentional inside a horizontal scroller
   * or clipper -- marquees, carousels, overflow-x:auto tables. body/html are
   * excluded: a page-level `overflow-x: hidden` is a common global reset, not a
   * deliberate scrolling component, and honouring it would mask real overflow. */
  function inScroller(el) {
    let n = el.parentElement;
    while (n && n !== document.body && n !== document.documentElement) {
      const ox = getComputedStyle(n).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true;
      n = n.parentElement;
    }
    return false;
  }

  const all = Array.from(document.querySelectorAll('body *'));
  const boxes = [];
  let offCanvas = 0;
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) continue;
    if (!onCanvas(r)) { offCanvas++; continue; }
    boxes.push({ el, r, text: hasDirectText(el) });
  }

  /* ---------- 1. overflow ---------- */
  /* Two distinct failures. A box can sit past the viewport edge, OR a box can
   * sit inside the viewport while its *content* spills out of it (long unwrapped
   * text). The second is invisible to a bounding-rect check -- rect.right stays
   * inside -- so it needs scrollWidth vs clientWidth. */
  const overflow = [];
  const contentOverflow = [];
  const docOverflow = document.documentElement.scrollWidth - VW;
  for (const b of boxes) {
    const scrolled = inScroller(b.el);
    const overRight = b.r.right - VW;
    if (!scrolled && (overRight > TOL || b.r.left < -TOL)) {
      overflow.push({
        path: pathOf(b.el),
        overflow_px: Math.round(Math.max(overRight, -b.r.left)),
        side: overRight > TOL ? 'right' : 'left',
        text: b.text ? (b.el.textContent || '').trim().slice(0, 60) : null,
      });
    }
    const ox = getComputedStyle(b.el).overflowX;
    // auto/scroll containers are meant to scroll; visible spills, hidden clips.
    if (!scrolled && (ox === 'visible' || ox === 'hidden') && b.el.clientWidth > 0) {
      const spill = b.el.scrollWidth - b.el.clientWidth;
      if (spill > 2) {
        contentOverflow.push({
          path: pathOf(b.el), spill_px: Math.round(spill),
          client_width: b.el.clientWidth, scroll_width: b.el.scrollWidth,
          clipped: ox === 'hidden',
          text: b.text ? (b.el.textContent || '').trim().slice(0, 60) : null,
        });
      }
    }
  }

  /* Two elements in different sibling panes that mutually cover each other are a
   * deliberate stack -- deck slides, carousel frames, tab panels, modals. Real
   * broken layout is a small thing landing on a big thing, so compare the
   * overlap against the LARGER pane: near-total mutual coverage means stacking,
   * while a badge sitting on a hero covers only a sliver of the hero. */
  const ancestry = el => { const c = []; let n = el; while (n) { c.push(n); n = n.parentElement; } return c; };
  function stackedPanes(a, b) {
    const ca = ancestry(a), cb = ancestry(b);
    const setB = new Set(cb);
    let i = 0;
    while (i < ca.length && !setB.has(ca[i])) i++;
    const common = ca[i];
    if (!common || i === 0) return false;
    const da = ca[i - 1], db = cb[cb.indexOf(common) - 1];
    if (!da || !db || da === db) return false;
    const ra = da.getBoundingClientRect(), rb = db.getBoundingClientRect();
    /* Two sibling containers occupying exactly the same box are stacked, even
     * when that box is degenerate. Deck slides collapse to zero height because
     * their children are absolutely positioned, so an area-ratio test alone
     * silently skips them -- measured on the open-design deck corpus. */
    const same = Math.abs(ra.left - rb.left) <= 1 && Math.abs(ra.top - rb.top) <= 1 &&
                 Math.abs(ra.width - rb.width) <= 1 && Math.abs(ra.height - rb.height) <= 1;
    if (same) return true;
    const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
    const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if (ox <= 0 || oy <= 0) return false;
    const biggest = Math.max(ra.width * ra.height, rb.width * rb.height);
    return biggest > 0 && (ox * oy) / biggest > 0.8;
  }

  /* ---------- 2. overlap (text-bearing, non-nested) ---------- */
  const overlaps = [];
  let overlapsTruncated = 0;
  const OVERLAP_CAP = 40; // a full dump would blow the model's context budget
  const txt = boxes.filter(b => b.text);
  outer:
  for (let i = 0; i < txt.length; i++) {
    for (let j = i + 1; j < txt.length; j++) {
      if (overlaps.length >= OVERLAP_CAP) { overlapsTruncated = txt.length - i; break outer; }
      const a = txt[i], b = txt[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      if (stackedPanes(a.el, b.el)) continue;
      const ox = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
      const oy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
      if (ox > TOL && oy > TOL) {
        const area = ox * oy;
        const smallest = Math.min(a.r.width * a.r.height, b.r.width * b.r.height);
        if (area / smallest > 0.10) {
          overlaps.push({
            a: pathOf(a.el), b: pathOf(b.el),
            overlap_px: Math.round(area),
            fraction_of_smaller: +(area / smallest).toFixed(2),
          });
        }
      }
    }
  }

  /* ---------- 2a2. surface collisions ---------- */
  /* Two elements that PAINT (background, border, shadow) intersecting is a
   * visual collision: a card drawn over a card, a sidebar bleeding across the
   * header, a button on a button. This is what human reviewers actually flag,
   * and it is invisible to a text-node comparison because the colliding things
   * are containers. Validated against 14 human-ranked artifacts: zero false
   * positives on accepted pages, and it flagged every page in the worst tier. */
  const _alpha = s => {
    const m = String(s).match(/rgba?\(([^)]+)\)/);
    if (!m) return 0;
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return p.length > 3 ? p[3] : 1;
  };
  function paints(el) {
    const cs = getComputedStyle(el);
    if (_alpha(cs.backgroundColor) > 0.05) return true;
    if (cs.backgroundImage && cs.backgroundImage !== 'none') return true;
    for (const s of ['Top', 'Right', 'Bottom', 'Left'])
      if (parseFloat(cs['border' + s + 'Width']) > 0 &&
          _alpha(cs['border' + s + 'Color']) > 0.05) return true;
    if (cs.boxShadow && cs.boxShadow !== 'none') return true;
    return false;
  }
  /* A layer covering essentially the whole viewport is a backdrop, not a
   * colliding element -- everything sits on top of it by design. */
  const VH = window.innerHeight;
  const isBackdrop = r => (r.width * r.height) >= 0.85 * VW * VH;

  /* Same-tag, same-class siblings that overlap are a deliberate idiom: stacked
   * avatar rows, fanned cards, overlapping chips built with negative margins.
   * Real collisions are between elements of DIFFERENT kinds. */
  function sameKindSiblings(a, b) {
    if (a.parentElement !== b.parentElement) return false;
    if (a.tagName !== b.tagName) return false;
    // Compare by SHARED class, not identical class strings: the idiom is a base
    // class plus a per-item modifier (`av av-cale`, `av a-dp`), so exact string
    // equality misses every real instance of it.
    const ca = (a.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean);
    const cb = (b.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean);
    if (!ca.length && !cb.length) return true;
    return ca.some(c => cb.includes(c));
  }

  const surfaces = boxes.filter(b => b.r.width >= 24 && b.r.height >= 16 &&
                                     !isBackdrop(b.r) && paints(b.el));
  const collisions = [];
  for (let i = 0; i < surfaces.length && collisions.length < 30; i++) {
    for (let j = i + 1; j < surfaces.length && collisions.length < 30; j++) {
      const a = surfaces[i], b = surfaces[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      if (stackedPanes(a.el, b.el)) continue;
      if (sameKindSiblings(a.el, b.el)) continue;
      const ox = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
      const oy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
      if (ox <= 1 || oy <= 1) continue;
      const area = ox * oy;
      const smallest = Math.min(a.r.width * a.r.height, b.r.width * b.r.height);
      const frac = area / smallest;
      if (frac > 0.02) {
        collisions.push({
          a: pathOf(a.el), b: pathOf(b.el),
          fraction: +frac.toFixed(3), overlap_px: Math.round(area),
          // geometry the model needs to reason about a fix
          a_rect: [Math.round(a.r.left), Math.round(a.r.top),
                   Math.round(a.r.width), Math.round(a.r.height)],
          b_rect: [Math.round(b.r.left), Math.round(b.r.top),
                   Math.round(b.r.width), Math.round(b.r.height)],
          dx: Math.round(ox), dy: Math.round(oy),
          a_tag: a.el.tagName.toLowerCase(), b_tag: b.el.tagName.toLowerCase(),
        });
      }
    }
  }
  collisions.sort((x, y) => y.fraction - x.fraction);

  /* ---------- 2a3. chart geometry ---------- */
  /* SVG internals are not DOM boxes, so a plotted line leaving its viewBox is
   * invisible to every element-level check -- the data is simply clipped away.
   * A human reviewer sees it instantly ("the line falls off the bottom"). */
  const chartFaults = [];
  for (const svg of document.querySelectorAll('svg[viewBox]')) {
    const vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
    if (vb.length !== 4 || vb.some(isNaN)) continue;
    const [vx, vy, vw, vh] = vb;
    const out = [];
    const note = (kind, x, y) => {
      if (x < vx - 1 || x > vx + vw + 1 || y < vy - 1 || y > vy + vh + 1)
        out.push({ kind, x: Math.round(x), y: Math.round(y) });
    };
    /* SVG `points` accepts commas and whitespace interchangeably: "50,300 250,250",
     * "50 300, 250 250" and "50 300 250 250" are all the same six coordinates.
     * Splitting on whitespace then commas silently parsed nothing for the middle
     * form -- which is exactly the form the model emitted, so a point 50 units
     * below the plot went undetected. Flatten to a number stream instead. */
    for (const el of svg.querySelectorAll('polyline, polygon')) {
      const nums = (el.getAttribute('points') || '')
        .split(/[\s,]+/).filter(Boolean).map(Number).filter(n => !isNaN(n));
      for (let k = 0; k + 1 < nums.length; k += 2)
        note(el.tagName.toLowerCase(), nums[k], nums[k + 1]);
    }
    for (const el of svg.querySelectorAll('rect')) {
      const x = +el.getAttribute('x'), y = +el.getAttribute('y');
      const w = +el.getAttribute('width'), h = +el.getAttribute('height');
      if (![x, y, w, h].some(isNaN)) { note('rect', x, y); note('rect', x + w, y + h); }
    }
    for (const el of svg.querySelectorAll('circle')) {
      const cx = +el.getAttribute('cx'), cy = +el.getAttribute('cy');
      if (![cx, cy].some(isNaN)) note('circle', cx, cy);
    }
    /* Only a data plot qualifies: a polyline/polygon with >2 vertices, or 3+
     * sibling rects (a bar series). Decorative icons have neither, and treating
     * them as unlabelled charts fired on every landing page in the sample. */
    let series = svg.querySelectorAll('rect').length >= 3;
    for (const el of svg.querySelectorAll('polyline, polygon')) {
      if ((el.getAttribute('points') || '').trim().split(/\s+/).length > 2) series = true;
    }
    const labels = svg.querySelectorAll('text').length;
    if (out.length || (series && labels === 0)) {
      chartFaults.push({
        path: pathOf(svg), viewBox: [vx, vy, vw, vh],
        outside: out.slice(0, 6), outside_count: out.length,
        text_labels: labels, is_series: series,
      });
    }
  }

  /* ---------- 2b. edge gutter ---------- */
  /* Text touching the viewport edge reads as broken even though it neither
   * overflows nor overlaps -- a generated page put its logo at x=0 and every
   * other check passed it. Only leaf text is considered: full-bleed section
   * backgrounds legitimately span edge to edge. */
  const edgeTouching = [];
  for (const b of boxes) {
    if (!b.text) continue;
    if (b.r.width >= VW - 2) continue;           // full-bleed by design
    /* Measure to where the TEXT starts, not the box. A block-level link inside a
     * padded sidebar has its box at x=0 while its content is inset -- the box
     * edge is not the visual gutter. */
    const pcs = getComputedStyle(b.el);
    const padL = parseFloat(pcs.paddingLeft) || 0;
    const padR = parseFloat(pcs.paddingRight) || 0;
    const left = b.r.left + padL, right = (VW - b.r.right) + padR;
    const gap = Math.min(left, right);
    if (gap < 0) continue;                        // that is overflow, not gutter
    if (gap < 8) {
      edgeTouching.push({
        path: pathOf(b.el), gap_px: Math.round(gap),
        side: left <= right ? 'left' : 'right',
        text: (b.el.textContent || '').trim().slice(0, 50),
      });
    }
  }

  /* ---------- 3. contrast ---------- */
  /* Report the measurement for every text element, not just those failing some
   * threshold. Thresholds are a grading policy and live in verify.py, so they
   * can be retuned without re-rendering the corpus. */
  const textContrast = [];
  for (const b of boxes) {
    if (!b.text) continue;
    if (textContrast.length >= 400) break;
    const cs = getComputedStyle(b.el);
    const fg = parseColor(cs.color);
    if (!fg || fg.a < 0.1) continue;
    const size = parseFloat(cs.fontSize) || 16;
    const weight = parseInt(cs.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    let worst = Infinity, worstBg = null;
    for (const bg of backdrops(b.el)) {
      const c = contrast(fg, bg);
      if (c < worst) { worst = c; worstBg = bg; }
    }
    if (!isFinite(worst)) continue;
    textContrast.push({
      path: pathOf(b.el), ratio: +worst.toFixed(2),
      fg: hex(fg), bg: worstBg ? hex(worstBg) : null,
      font_px: +size.toFixed(1), weight, large,
      text: (b.el.textContent || '').trim().slice(0, 60),
    });
  }

  /* ---------- 4. palette membership ---------- */
  const palette = (CFG.palette || []).map(s => {
    const c = parseColor(s); return c ? hex(c) : null;
  }).filter(Boolean);
  const offPalette = [];
  if (palette.length) {
    const seen = new Map();
    for (const b of boxes) {
      const cs = getComputedStyle(b.el);
      for (const prop of ['color', 'backgroundColor', 'borderTopColor']) {
        const c = parseColor(cs[prop]);
        if (!c || c.a < 0.05) continue;
        const h = hex(c);
        if (palette.includes(h)) continue;
        if (!seen.has(h)) seen.set(h, { color: h, prop, count: 0, example: pathOf(b.el) });
        seen.get(h).count++;
      }
    }
    offPalette.push(...Array.from(seen.values()).sort((x, y) => y.count - x.count));
  }

  /* ---------- 5. required components ---------- */
  const components = {};
  for (const sel of (CFG.components || [])) {
    let found = 0;
    try {
      found = document.querySelectorAll(
        sel.startsWith('.') || sel.startsWith('#') || sel.startsWith('[') ? sel : `[data-od-id="${sel}"], ${sel}`
      ).length;
    } catch (e) { found = -1; }
    components[sel] = found;
  }

  /* ---------- 6. tap targets ---------- */
  /* Same policy split: report sizes, let the grader apply the minimum. */
  const targets = [];
  for (const b of boxes) {
    const t = b.el.tagName.toLowerCase();
    if (!['a', 'button', 'input', 'select', 'textarea'].includes(t)) continue;
    // WCAG 2.2 SC 2.5.8 exempts targets in a sentence or inline text flow.
    const inline = getComputedStyle(b.el).display.startsWith('inline');
    targets.push({
      path: pathOf(b.el), inline,
      size: [Math.round(b.r.width), Math.round(b.r.height)],
    });
  }

  return {
    viewport: [VW, window.innerHeight],
    doc_scroll_width: document.documentElement.scrollWidth,
    doc_overflow_px: docOverflow > TOL ? Math.round(docOverflow) : 0,
    elements_examined: boxes.length,
    off_canvas_skipped: offCanvas,
    overflow, content_overflow: contentOverflow,
    overlaps, overlaps_truncated: overlapsTruncated,
    surface_collisions: collisions,
    chart_faults: chartFaults,
    edge_touching: edgeTouching,
    text_contrast: textContrast,
    off_palette: offPalette, components, targets,
  };
}

  /* Always emit a tag. A probe that dies silently is indistinguishable from a
   * clean page, which would turn a crash into a false pass. */
  let report;
  try {
    report = collect();
  } catch (e) {
    report = { probe_error: String((e && e.stack) || e) };
  }
  const tag = document.createElement('script');
  tag.type = 'application/json';
  tag.id = '__od_audit__';
  // escape < so a stray closing script tag inside page text can't end this early
  tag.textContent = JSON.stringify(report).replace(/</g, '\\u003c');
  document.body.appendChild(tag);
})();
