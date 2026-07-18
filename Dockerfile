# Designer Agent -- takes kind=design tasks and produces verified HTML artifacts.
#
# The image carries everything needed to RENDER and JUDGE an artifact: Chromium
# for the geometry audit and screenshots, and the fonts the design systems
# specify. Fonts are baked in deliberately -- a missing family renders as a
# fallback, which silently misrepresents the design to the vision tier.
#
# What is NOT in the image: design systems, templates, tasks and run output.
# Those live in ./data and are bind-mounted, so packages can be added or
# repaired without a rebuild.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DESIGNER_DATA=/app/data \
    DESIGNER_CHROME=/usr/bin/chromium

# chromium: headless renderer for the audit probe and screenshots.
# fontconfig + base faces: without these every page renders in a fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation2 \
    && rm -rf /var/lib/apt/lists/*

# The families the bundled design systems actually name (Inter is used by ~93
# of the 151 packages). OFL/Apache licensed, see fonts/README.md.
COPY fonts/ /usr/local/share/fonts/designer/
RUN fc-cache -f && fc-list : family | sort -u | wc -l

WORKDIR /app
COPY src/ /app/src/

# Test stage input: fixtures are code-adjacent, not data, so they ship in the
# image and the render pipeline can be smoke-tested without the bind mount.
COPY fixtures/ /app/fixtures/
COPY run_check.py corpus_run.py /app/

ENV PYTHONPATH=/app/src

# Fail the build if the renderer cannot resolve Chromium or produce an audit.
RUN python3 -c "\
import sys; sys.path.insert(0,'/app/src');\
from designer import render, verify;\
a=render.audit('/app/fixtures/good.html');\
f=verify.tier2(a);\
assert a['elements_examined']>0, 'audit probe produced nothing';\
print('render smoke test OK:', a['elements_examined'], 'elements,', len(f), 'findings')"

ENTRYPOINT ["python3", "-m", "designer.cli"]
CMD ["--help"]
