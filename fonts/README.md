# Bundled fonts

Baked into the image (not the bind mount) because a missing family renders as a
fallback, which silently misrepresents the design to the vision tier.

| Family | Files | License | Why |
|---|---|---|---|
| Inter | `Inter-{Regular,Medium,SemiBold,Bold}.ttf`, `InterVariable.ttf` | SIL OFL 1.1 | Named by ~93 of the 151 bundled design systems |
| JetBrains Mono | `JetBrainsMono-{Regular,Bold}.ttf` | SIL OFL 1.1 | Most common free mono in the token stacks |

**The static faces are required, not redundant.** `InterVariable.ttf` registers
its family as "Inter Variable"; a stack asking for `font-family: Inter` does NOT
match it and silently falls back to serif. Verified by measuring rendered text
width in headless Chromium -- with only the variable file, `Inter` measured
261.1px, identical to a nonexistent font.

Proprietary families the design systems also name (Menlo, SF Mono, Consolas,
Segoe UI, Helvetica) are macOS/Windows-only by design and are expected to fall
through to the generic terminator in each stack.
