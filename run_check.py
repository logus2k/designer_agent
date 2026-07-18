import json, sys
sys.path.insert(0, "src")
from designer import render, verify

PALETTE = ["#fafaf9","#1c1b1a","#6b6964","#e6e4e0","#c96442","#ffffff"]
COMPONENTS = ["header","h1","button"]

for name in ("good","bad"):
    path = f"fixtures/{name}.html"
    a = render.audit(path, palette=PALETTE, components=COMPONENTS)
    html = open(path, encoding="utf-8").read()
    f = verify.tier1(html) + verify.tier2(a, COMPONENTS)
    s = verify.score(f)
    print(f"\n{'='*22} {name}.html {'='*22}")
    print(f"examined={a['elements_examined']} off_canvas={a['off_canvas_skipped']} "
          f"doc_overflow={a['doc_overflow_px']}px overflow={len(a['overflow'])} "
          f"overlaps={len(a['overlaps'])} text_nodes={len(a['text_contrast'])} "
          f"off_palette={len(a['off_palette'])}")
    print(f"SCORE: {json.dumps(s)}")
    print(verify.render_for_agent(f))
