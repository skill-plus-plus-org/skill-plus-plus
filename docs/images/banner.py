"""Generate the README's banner as two SVGs (light, dark).

    python3 docs/images/banner.py

Three runs of the same work (the stage colours of how_it_works.py) fold into
one SKILL.md, beside the wordmark. Positions come from the constants below.
"""
from pathlib import Path

W, H = 880, 170
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

THEMES = {
    "light": {"text": "#0f172a", "muted": "#475569", "card": "#ffffff", "line": "#e2e8f0",
              "arrow": "#94a3b8", "runs": ("#2563eb", "#7c3aed", "#059669"),
              "tints": ("#eff6ff", "#f5f3ff", "#ecfdf5"), "accent": "#059669"},
    "dark": {"text": "#e6edf3", "muted": "#9aa7b4", "card": "#0d1117", "line": "#30363d",
             "arrow": "#6e7681", "runs": ("#58a6ff", "#a78bfa", "#3fb950"),
             "tints": ("#0c1b33", "#1a1333", "#0b2419"), "accent": "#3fb950"},
}

RUN_W, RUN_H = 92, 50          # one recorded run
RUN_STEP = 24                  # each run sits this far below and right of the last
SKILL_W, SKILL_H = 104, 122    # the SKILL.md it becomes


def build(theme: str) -> str:
    t = THEMES[theme]
    out = []
    # 128: centres the whole group, measured at 623 px wide with the system font
    # (fonts elsewhere differ by a few pixels; it stays near the middle).
    x0, y0 = 128, (H - (RUN_H + 2 * RUN_STEP)) / 2
    for i in range(3):             # the repeats, back to front
        x, y = x0 + i * RUN_STEP, y0 + i * RUN_STEP
        out.append(f'<rect x="{x}" y="{y}" width="{RUN_W}" height="{RUN_H}" rx="9" '
                   f'fill="{t["tints"][i]}" stroke="{t["runs"][i]}" stroke-width="1.4"/>')
        for k, w in enumerate((0.62, 0.42)):
            out.append(f'<rect x="{x + 12}" y="{y + 14 + k * 12}" width="{RUN_W * w:.0f}" '
                       f'height="5" rx="2.5" fill="{t["runs"][i]}" opacity="0.55"/>')
    runs_right = x0 + 2 * RUN_STEP + RUN_W
    mid = H / 2
    ax1, ax2 = runs_right + 16, runs_right + 52
    out.append(f'<line x1="{ax1}" y1="{mid}" x2="{ax2 - 6}" y2="{mid}" stroke="{t["arrow"]}" '
               f'stroke-width="2" stroke-linecap="round"/>')
    out.append(f'<path d="M{ax2 - 8},{mid - 6} L{ax2},{mid} L{ax2 - 8},{mid + 6}" fill="none" '
               f'stroke="{t["arrow"]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')
    sx, sy = ax2 + 16, (H - SKILL_H) / 2       # the skill
    out.append(f'<rect x="{sx}" y="{sy}" width="{SKILL_W}" height="{SKILL_H}" rx="11" '
               f'fill="{t["card"]}" stroke="{t["accent"]}" stroke-width="1.8"/>')
    out.append(f'<text x="{sx + 14}" y="{sy + 26}" font-family="{MONO}" font-size="12" '
               f'font-weight="600" fill="{t["accent"]}">SKILL.md</text>')
    for k in range(4):
        cy = sy + 46 + k * 17
        out.append(f'<circle cx="{sx + 19}" cy="{cy}" r="3.2" fill="{t["accent"]}"/>')
        out.append(f'<rect x="{sx + 29}" y="{cy - 2.5}" width="{SKILL_W - 45 - (k % 2) * 16}" '
                   f'height="5" rx="2.5" fill="{t["line"]}"/>')
    wx = sx + SKILL_W + 48                      # the wordmark
    out.append(f'<text x="{wx}" y="{mid + 14}" font-family="{FONT}" font-size="64" '
               f'font-weight="800" letter-spacing="-1.5">'
               f'<tspan fill="{t["text"]}">skill</tspan><tspan fill="{t["accent"]}">pp</tspan></text>')
    out.append(f'<text x="{wx + 2}" y="{mid + 44}" font-family="{MONO}" font-size="14" '
               f'fill="{t["muted"]}">repeated work → reviewed skills</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}" role="img" aria-label="skillpp: repeated work becomes a reviewed skill">'
            f'<title>skillpp</title>' + "".join(out) + "</svg>\n")


here = Path(__file__).parent
for theme in THEMES:
    (here / f"banner-{theme}.svg").write_text(build(theme))
