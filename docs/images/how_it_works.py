"""Generate the README's how-it-works visual as two SVGs (light, dark).

    python3 docs/images/how_it_works.py

The README shows one or the other with <picture>, by the reader's theme. Edit
the text in STAGES, then run this; keep what each box says true to the code.

Every position is computed from the constants below, so boxes in a row share
one width, rows share one height, and gaps are equal.
"""
from html import escape
from pathlib import Path

W = 880                      # canvas width, GitHub's README column
PAD = 20                     # canvas margin
CARD_PAD = 20                # inside a stage card
HEAD_H = 44                  # stage header row
BOX_H = 104                  # every step box, room for a model label at the bottom
ARROW = 30                   # gap between boxes, holds the arrow
LINK_H = 46                  # gap between stages, holds the connector
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

STAGES = [
    {"key": "capture", "n": "1", "title": "Capture", "sub": "while you work",
     "chip": "Claude Code · CLI or desktop app",
     "steps": [("Hooks", ["your prompts, the tools", "that ran, the replies"], None),
               ("Scrub", ["secrets, tokens and", "emails removed first"], None),
               ("Session buffer", ["one file per session,", "on your machine"], None)],
     "link": "session ends: /exit, /clear or quitting · missed ones are picked up at the next start"},
    {"key": "fold", "n": "2", "title": "Fold", "sub": "in the background, after the session",
     "chip": "local models via Ollama",
     "steps": [("Cut", ["a new task at", "this prompt?"], "gemma4:e4b"),
               ("Segment", ["split into tasks;", "look-only work dropped"], None),
               ("Extract", ["steps, conversation", "and dependencies"], None),
               ("Match", ["same procedure as", "a candidate?"], "nomic-embed-text")],
     "link": "the same procedure again: count +1 · anything else: a new candidate"},
    {"key": "review", "n": "3", "title": "Review and draft", "sub": "when you choose",
     "chip": "your agent · only when you ask",
     "steps": [("Ledger", ["counts, evidence", "and your decisions"], None),
               ("Review page", ["ready at 3× · promote", "or dismiss"], None),
               ("Draft Skill", ["your agent writes", "SKILL.md from one run"], "claude -p"),
               ("You", ["answer its questions,", "download, install"], None)],
     "link": None},
]

THEMES = {
    "light": {
        "text": "#0f172a", "muted": "#475569", "box": "#ffffff", "boxline": "#e2e8f0",
        "arrow": "#94a3b8", "chipbg": "#ffffff", "codebg": "#f1f5f9", "codetext": "#334155",
        "capture": ("#eff6ff", "#bfdbfe", "#2563eb"),
        "fold": ("#f5f3ff", "#ddd6fe", "#7c3aed"),
        "review": ("#ecfdf5", "#a7f3d0", "#059669"),
    },
    "dark": {
        "text": "#e6edf3", "muted": "#9aa7b4", "box": "#0d1117", "boxline": "#30363d",
        "arrow": "#6e7681", "chipbg": "#0d1117", "codebg": "#161b22", "codetext": "#c9d1d9",
        "capture": ("#0c1b33", "#1f3b6e", "#58a6ff"),
        "fold": ("#1a1333", "#3d2a73", "#a78bfa"),
        "review": ("#0b2419", "#1b5e40", "#3fb950"),
    },
}


def text(x, y, s, size, fill, weight=400, anchor="start", family=FONT):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>')


def build(theme: str) -> str:
    t = THEMES[theme]
    out, y = [], PAD
    inner = W - 2 * PAD - 2 * CARD_PAD
    for i, st in enumerate(STAGES):
        fill, line, accent = t[st["key"]]
        card_h = CARD_PAD + HEAD_H + BOX_H + CARD_PAD
        out.append(f'<rect x="{PAD}" y="{y}" width="{W - 2 * PAD}" height="{card_h}" rx="16" '
                   f'fill="{fill}" stroke="{line}" stroke-width="1"/>')
        # header: number badge, title, subtitle, chip on the right
        cx, cy = PAD + CARD_PAD + 14, y + CARD_PAD + 14
        out.append(f'<circle cx="{cx}" cy="{cy}" r="14" fill="{accent}"/>')
        out.append(text(cx, cy + 5, st["n"], 14, "#ffffff", 700, "middle"))
        # One text element, so the subtitle follows the title at the same gap
        # whatever the title's rendered width.
        out.append(f'<text x="{cx + 24}" y="{cy + 6}" font-family="{FONT}">'
                   f'<tspan font-size="17" font-weight="700" fill="{t["text"]}">{escape(st["title"])}</tspan>'
                   f'<tspan dx="10" font-size="13" fill="{t["muted"]}">{escape(st["sub"])}</tspan></text>')
        chip_w = len(st["chip"]) * 6.6 + 24
        chip_x = W - PAD - CARD_PAD - chip_w
        out.append(f'<rect x="{chip_x:.1f}" y="{cy - 12}" width="{chip_w:.1f}" height="24" rx="12" '
                   f'fill="{t["chipbg"]}" stroke="{line}"/>')
        out.append(text(chip_x + chip_w / 2, cy + 4, st["chip"], 11.5, accent, 600, "middle"))
        # step boxes: equal widths, equal gaps
        n = len(st["steps"])
        bw = (inner - (n - 1) * ARROW) / n
        by = y + CARD_PAD + HEAD_H
        for j, (title, lines, code) in enumerate(st["steps"]):
            bx = PAD + CARD_PAD + j * (bw + ARROW)
            out.append(f'<rect x="{bx:.1f}" y="{by}" width="{bw:.1f}" height="{BOX_H}" rx="10" '
                       f'fill="{t["box"]}" stroke="{t["boxline"]}"/>')
            out.append(f'<rect x="{bx:.1f}" y="{by + 12}" width="3" height="{BOX_H - 24}" rx="1.5" fill="{accent}"/>')
            tx = bx + 16
            out.append(text(tx, by + 26, title, 14, t["text"], 700))
            for k, l in enumerate(lines):
                out.append(text(tx, by + 46 + k * 17, l, 12.5, t["muted"]))
            if code:
                # On its own line at the bottom, left-aligned with the text:
                # beside the title it covered "Match" in a quarter-width box.
                cw = len(code) * 6.9 + 14
                out.append(f'<rect x="{tx:.1f}" y="{by + BOX_H - 30}" width="{cw:.1f}" height="19" rx="5" fill="{t["codebg"]}"/>')
                out.append(text(tx + cw / 2, by + BOX_H - 16.5, code, 11, t["codetext"], 500, "middle", MONO))
            if j < n - 1:
                ax1, ax2, ay = bx + bw + 6, bx + bw + ARROW - 6, by + BOX_H / 2
                out.append(f'<line x1="{ax1:.1f}" y1="{ay}" x2="{ax2 - 5:.1f}" y2="{ay}" '
                           f'stroke="{t["arrow"]}" stroke-width="1.6" stroke-linecap="round"/>')
                out.append(f'<path d="M{ax2 - 6:.1f},{ay - 4} L{ax2:.1f},{ay} L{ax2 - 6:.1f},{ay + 4}" '
                           f'fill="none" stroke="{t["arrow"]}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>')
        y += card_h
        if st["link"]:
            mx = PAD + CARD_PAD + 14      # in line with the number badges
            out.append(f'<line x1="{mx}" y1="{y + 4}" x2="{mx}" y2="{y + LINK_H - 8}" '
                       f'stroke="{t["arrow"]}" stroke-width="1.6" stroke-linecap="round"/>')
            out.append(f'<path d="M{mx - 5},{y + LINK_H - 12} L{mx},{y + LINK_H - 6} L{mx + 5},{y + LINK_H - 12}" '
                       f'fill="none" stroke="{t["arrow"]}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>')
            out.append(text(mx + 16, y + LINK_H / 2 + 4, st["link"], 12.5, t["muted"], 400, "start"))
            y += LINK_H
    h = y + PAD
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" '
            f'role="img" aria-label="How Skill++ works: capture, fold, review and draft">'
            f'<title>How Skill++ works</title>' + "".join(out) + "</svg>\n")


here = Path(__file__).parent
for theme in THEMES:
    (here / f"how-it-works-{theme}.svg").write_text(build(theme))
