"""Generate the README's numbers visual as two SVGs (light, dark).

    python3 docs/images/numbers.py

The README shows one or the other with <picture>, by the reader's theme, at the
top of "The numbers". Every figure is one the text below it gives, from the last
measurement (the notes in tests/fixtures/sessions/expected.json): change TILES
when that changes, then run this.

One dot per case: a recording, a task switch, a prompt that had to stay in its
task. The colours are the diagram's (how_it_works.py): emerald, the review
page's colour for an installed skill, for what came out right, and a neutral
grey for the prompts that stayed whole, since there is no false cut to colour.
The last tile is the one that matters most, so it carries the emerald frame.
"""
from html import escape
from pathlib import Path

W = 880                      # canvas width, GitHub's README column
PAD = 20                     # canvas margin
GAP = 16                     # between tiles
TILE_W = (W - 2 * PAD - 3 * GAP) / 4
TILE_H = 272
INSET = 18                   # inside a tile
DOT, PITCH, COLS = 8, 12, 10
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

# label, figure, out of, dots, caption lines; the last tile draws the merge
TILES = [
    ("Sessions cut right", "30", "/30", 30, ["One dot per recording"]),
    ("Task switches caught", "13", "/13", 13, ["One dot per switch"]),
    ("False cuts", "0", "/100", 100, ["One dot per prompt that", "had to stay in its task"]),
    ("Wrong merges", "0", "", 0, ["7 sprint reviews became", "1 candidate"]),
]

THEMES = {
    "light": {
        "text": "#0f172a", "muted": "#475569", "box": "#ffffff", "boxline": "#e2e8f0",
        "dot": "#cbd5e1", "badge": "#ffffff",
        "review": ("#ecfdf5", "#a7f3d0", "#059669"),
    },
    "dark": {
        "text": "#e6edf3", "muted": "#9aa7b4", "box": "#0d1117", "boxline": "#30363d",
        "dot": "#3d444d", "badge": "#0d1117",
        "review": ("#122827", "#1d5f4b", "#34d399"),
    },
}


def text(x, y, s, size, fill, weight=400, anchor="start"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>')


def dots(x, y, n, fill):
    return [f'<rect x="{x + (i % COLS) * PITCH:.1f}" y="{y + (i // COLS) * PITCH:.1f}" '
            f'width="{DOT}" height="{DOT}" rx="{DOT / 2}" fill="{fill}"/>' for i in range(n)]


def merge(x, y, accent):
    """Seven runs drawn into one candidate."""
    bx, by = x + 120, y + 33
    runs = [(x + 4, y + 3 + i * 10) for i in range(7)]
    out = [f'<line x1="{rx:.1f}" y1="{ry:.1f}" x2="{bx:.1f}" y2="{by:.1f}" stroke="{accent}" '
           f'stroke-opacity="0.35" stroke-width="1.2"/>' for rx, ry in runs]
    out += [f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="{DOT / 2}" fill="{accent}"/>' for rx, ry in runs]
    out.append(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="11" fill="{accent}"/>')
    return out


def build(theme: str) -> str:
    t = THEMES[theme]
    tint, _, accent = t["review"]
    out = []
    for n, (label, figure, out_of, count, caption) in enumerate(TILES):
        x, y = PAD + n * (TILE_W + GAP), PAD
        last = n == len(TILES) - 1
        edge = 1 if last else 0.5
        out.append(f'<rect x="{x + edge:.1f}" y="{y + edge:.1f}" width="{TILE_W - 2 * edge:.1f}" '
                   f'height="{TILE_H - 2 * edge:.1f}" rx="12" fill="{tint if last else t["box"]}" '
                   f'stroke="{accent if last else t["boxline"]}" stroke-width="{2 if last else 1}"/>')
        out.append(text(x + INSET, y + 32, label, 14, t["muted"], 500))
        if last:
            bw = 92
            bx = x + TILE_W - INSET - bw
            out.append(f'<rect x="{bx:.1f}" y="{y + 46}" width="{bw}" height="22" rx="11" '
                       f'fill="{accent}"/>')
            out.append(text(bx + bw / 2, y + 61, "matters most", 12, t["badge"], 600, "middle"))
        rest = (f'<tspan font-size="24" font-weight="500" fill="{t["muted"]}">{out_of}</tspan>'
                if out_of else "")
        out.append(f'<text x="{x + INSET:.1f}" y="{y + 86}" font-family="{FONT}" font-size="44" '
                   f'font-weight="700" fill="{t["text"]}">{figure}{rest}</text>')
        if last:
            out += merge(x + INSET, y + 110, accent)
        else:
            out += dots(x + INSET, y + 106, count, accent if out_of != "/100" else t["dot"])
        for i, line in enumerate(reversed(caption)):
            out.append(text(x + INSET, y + TILE_H - 16 - i * 16, line, 12, t["muted"]))
    H = TILE_H + 2 * PAD
    alt = ("30 of 30 sessions cut right, 13 of 13 task switches caught, 0 false cuts "
           "in 100 prompts, 0 wrong merges: the seven sprint reviews became one candidate")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}" role="img" aria-label="{alt}">'
            f'<title>The numbers</title>' + "".join(out) + "</svg>\n")


here = Path(__file__).parent
for theme in THEMES:
    (here / f"numbers-{theme}.svg").write_text(build(theme))
