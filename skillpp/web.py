"""A local page over the ledger, for reading it rather than listing it.

Rewritten rather than ported. The previous version predates `hint`,
`description`, `parked_at_occurrences`, the `one-off` and `split` statuses and
the decisions log, so rendering the current ledger through it would have shown
parked entries as live ones and no ranking at all — a stale view that looks
authoritative, which is worse than no view.

Binds to 127.0.0.1 and writes real files under the skills directory, so it is a
local tool with no authentication and must never be exposed. Skill names are
matched against a pattern and paths outside the roots are refused rather than
sanitised.

Dependency-free on purpose: `http.server` and one self-contained page, no
bundler and no build step, consistent with the rest of the tool.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .episode import render_step
from .ledger import (STATUS_CANDIDATE, STATUS_DISMISSED, STATUS_ONE_OFF,
                     STATUS_PROMOTED, STATUS_SPLIT, Ledger)
from .lifecycle import parse_frontmatter

PROMPTS = Path(__file__).resolve().parent / "prompts"
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PARKED = (STATUS_DISMISSED, STATUS_ONE_OFF)


def _skill_path(skills_dir: Path, config: Config, name: str) -> Path | None:
    """Resolve a skill name to a file, refusing anything outside the roots."""
    if not _SAFE_NAME.match(name or ""):
        return None
    for root in (skills_dir, config.cold_dir, config.archive_dir):
        candidate = (root / name / "SKILL.md").resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def save_skill(path: Path, text: str) -> dict:
    """Overwrite a skill, but only if it would still load.

    Validated before the write, not after: a skill whose frontmatter is broken
    is not a skill, and discovering that on the next session start is too late.
    The previous version is kept beside it.
    """
    front = parse_frontmatter(text)
    if not front.get("name"):
        return {"ok": False, "error": "frontmatter has no name"}
    description = str(front.get("description") or "")
    if not description:
        return {"ok": False, "error": "frontmatter has no description — it is "
                                      "the only thing read when deciding "
                                      "whether to load a skill"}
    if len(description) > 200:
        return {"ok": False, "error": f"description is {len(description)} "
                                      f"characters; the limit is 200"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, path.with_suffix(f".md.bak-{stamp}"))
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "backup": path.with_suffix(f".md.bak-{stamp}").name}


def _entry_json(entry, threshold: int) -> dict:
    return {
        "id": entry.id,
        "title": entry.title,
        "description": entry.description,
        "status": entry.status,
        "hint": entry.hint,
        "occurrences": entry.occurrences,
        "ready": entry.ready(threshold),
        "source": entry.source,
        "last_seen": entry.last_seen[:10],
        "steps": [render_step(s) for s in entry.steps[:40]],
        "step_count": len(entry.steps),
        "intents": entry.intents[:3],
        "deps": sorted(set(entry.deps_cli) | set(entry.deps_mcp)),
        "skill_path": entry.skill_path,
        "since_parked": entry.recurrences_since_parked(),
        "parking_looks_wrong": entry.parking_looks_wrong(threshold),
    }


REVIEW_TAGS = ("good", "duplicate", "fragment")


def _review_path(config: Config) -> Path:
    return config.root / "review.json"


def load_review(config: Config) -> dict:
    """Tags a person put on candidates while reading them. Never read by the
    pipeline — this is for judging what detection produced, not steering it."""
    try:
        data = json.loads(_review_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_review(config: Config, entry_id: str, tag: str) -> dict:
    """Record one tag. An empty tag clears it."""
    if tag and tag not in REVIEW_TAGS:
        return {"ok": False, "error": f"unknown tag {tag!r}"}
    if not Ledger(config).get(entry_id):
        return {"ok": False, "error": "no such entry"}
    tags = load_review(config)
    if tag:
        tags[entry_id] = tag
    else:
        tags.pop(entry_id, None)
    path = _review_path(config)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tags, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True}


def _summaries_path(config: Config) -> Path:
    return config.root / "review_summaries.json"


def load_summaries(config: Config) -> dict:
    try:
        data = json.loads(_summaries_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cached_summary(cache: dict, entry) -> str:
    """The cached sentence, if it still describes the entry as it stands."""
    hit = cache.get(entry.id) or {}
    return hit.get("text", "") if hit.get("steps") == len(entry.steps) else ""


def summarise(config: Config, entry_id: str) -> dict:
    """One plain sentence saying what a candidate's work was about.

    Titles cannot carry this: 24 of 28 real candidate titles are just something
    the developer typed — "option 1", "i didnt stop anything" — and the latter
    turned out to be generating a hero image. The sentence is written from the
    prompts, the assistant's own completion reports, and the first steps.

    Cached per entry and keyed on step count, so the page never waits on a model
    to load and a candidate that grows is described again. Stored beside the
    ledger rather than in `entry.description`, which decides whether a promoted
    skill loads and must not be filled in by a review aid.
    """
    import re

    from .boundary import render_step as step_line
    from .local import LocalModelUnavailable, ask

    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    cache = load_summaries(config)
    cached = _cached_summary(cache, entry)
    if cached:
        return {"ok": True, "summary": cached}

    asks = "\n".join(f"- {i.strip()[:200]}" for i in entry.intents[:6] if i.strip())
    reports = [st["closing_note"][:200] for st in entry.steps if st.get("closing_note")]
    steps = "\n".join(f"- {step_line(st)[:140]}" for st in entry.steps[:12])
    prompt = ((PROMPTS / "candidate_summary.md").read_text(encoding="utf-8")
              .replace("{ASKS}", asks or "- (none recorded)")
              .replace("{REPORTS}", "\n".join(f"- {r}" for r in reports[:4]) or "- (none)")
              .replace("{STEPS}", steps or "- (none)"))
    try:
        reply = ask(config.local_model, prompt, host=config.ollama_url,
                    timeout=90.0, think=False)
    except LocalModelUnavailable as exc:
        return {"ok": False, "error": f"no local model: {exc}"}

    # The model sometimes appends labelled blocks after the sentence, and opens
    # with "The developer" despite being told to start with a verb.
    line = next((l for l in reply.strip().splitlines() if l.strip()), "")
    text = re.sub(r"^(the )?developer\s+", "", line.strip().strip("*"),
                  flags=re.IGNORECASE).strip()
    if not text:
        return {"ok": False, "error": "the model returned nothing"}
    text = text[0].upper() + text[1:]

    cache[entry.id] = {"steps": len(entry.steps), "text": text}
    path = _summaries_path(config)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "summary": text}


def _title_is_a_prompt(title: str, intents: list[str]) -> bool:
    head = (title or "").rstrip("…").strip()
    if not head:
        return False
    return any(i.strip().splitlines()[0].startswith(head)
               for i in intents if i and i.strip())


def review_rows(config: Config) -> list[dict]:
    """Every live candidate, with what a person needs to judge it.

    `closest` is shown rather than used: lexical similarity does not see
    near-duplicates reliably — four "restart the servers" candidates in the real
    ledger sit 0.25-0.46 apart — so grouping behind a threshold would hide as
    much as it showed. The score is there for the reader to weigh.

    `title_from_prompt` marks a title that is just something the developer
    typed. Compared against the prompts themselves rather than inferred from
    whether a commit exists: a title is fixed when the entry is created, so a
    later commit in the steps says nothing about where the title came from — a
    68-step debugging session titled "restart again" has a commit in it.
    """
    from .normalize import signature
    from .recurrence import similarity

    entries = [e for e in Ledger(config).all() if e.status == STATUS_CANDIDATE]
    sigs = {e.id: (e.signature or signature(e.steps)) for e in entries}
    tags = load_review(config)
    summaries = load_summaries(config)
    rows = []
    for e in entries:
        others = [(similarity(sigs[e.id], sigs[o.id]), o)
                  for o in entries if o.id != e.id]
        score, near = max(others, key=lambda pair: pair[0]) if others else (0.0, None)
        rows.append({
            "id": e.id,
            "title": e.title,
            "title_from_prompt": _title_is_a_prompt(e.title, e.intents),
            "occurrences": e.occurrences,
            "ready": e.ready(config.recurrence_threshold),
            "step_count": len(e.steps),
            "intents": list(e.intents),
            "steps": [render_step(st) for st in e.steps[:200]],
            "last_seen": e.last_seen[:10],
            "closest": ({"id": near.id, "title": near.title,
                         "score": round(score, 2)} if near else None),
            "tag": tags.get(e.id, ""),
            "summary": _cached_summary(summaries, e),
            "reports": [st["closing_note"] for st in e.steps
                        if st.get("closing_note")],
        })
    return sorted(rows, key=lambda r: (r["title"] or "").lower())


def collect_state(config: Config, skills_dir: Path) -> dict:
    """Everything the page shows, in one read."""
    from . import decisions
    from .lifecycle import reconcile

    ledger = Ledger(config)
    threshold = config.recurrence_threshold
    entries = list(ledger.all())

    skills = []
    for folder in sorted(p for p in skills_dir.glob("*") if p.is_dir()):
        path = folder / "SKILL.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        front = parse_frontmatter(text)
        owner = next((e for e in entries
                      if e.skill_path and Path(e.skill_path).name == "SKILL.md"
                      and Path(e.skill_path).parent.name == folder.name), None)
        skills.append({
            "name": folder.name,
            "description": str(front.get("description") or ""),
            "body": text,
            "provenance": owner.id if owner else "",
            "occurrences": owner.occurrences if owner else 0,
        })

    return {
        "skills": skills,
        "candidates": [_entry_json(e, threshold) for e in entries
                       if e.status == STATUS_CANDIDATE],
        "parked": [_entry_json(e, threshold) for e in entries
                   if e.status in _PARKED],
        "promoted": [_entry_json(e, threshold) for e in entries
                     if e.status == STATUS_PROMOTED],
        "split": [_entry_json(e, threshold) for e in entries
                  if e.status == STATUS_SPLIT],
        "review": review_rows(config),
        "threshold": threshold,
        "accuracy": decisions.score(config),
        "drift": reconcile(ledger, config),
        "root": str(config.root),
        "skills_dir": str(skills_dir),
    }


def reopen(config: Config, entry_id: str) -> dict:
    """Put a parked candidate back. The only mutation the page makes to status.

    Deliberately the one direction offered: parking is a decision and a page
    should not quietly reverse it in bulk, but a parking that recurred is
    exactly what someone opens this to look at.
    """
    ledger = Ledger(config)
    entry = ledger.get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    if entry.status not in _PARKED:
        return {"ok": False, "error": f"{entry.id} is {entry.status}"}
    entry.status = STATUS_CANDIDATE
    ledger.save(entry)
    return {"ok": True}


def make_handler(config: Config, skills_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass                                    # quiet: this is a local page

        def _send(self, code, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps(
                    collect_state(config, skills_dir)))
            self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad json"}))

            if self.path == "/api/skill/save":
                path = _skill_path(skills_dir, config, payload.get("name", ""))
                if not path:
                    return self._send(400, json.dumps(
                        {"ok": False, "error": "unknown skill"}))
                return self._send(200, json.dumps(
                    save_skill(path, payload.get("body", ""))))

            if self.path == "/api/review/summary":
                return self._send(200, json.dumps(
                    summarise(config, str(payload.get("id", "")))))

            if self.path == "/api/review":
                return self._send(200, json.dumps(save_review(
                    config, str(payload.get("id", "")),
                    str(payload.get("tag", "")))))

            if self.path == "/api/reopen":
                return self._send(200, json.dumps(
                    reopen(config, payload.get("id", ""))))

            self._send(404, json.dumps({"error": "not found"}))

    return Handler


def serve(config: Config, skills_dir: Path, port: int = 8765,
          open_browser: bool = True) -> ThreadingHTTPServer:
    """Serve on loopback only. Never bind anywhere else: there is no auth."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                make_handler(config, skills_dir))
    if open_browser:
        threading.Thread(target=webbrowser.open, daemon=True,
                         args=[f"http://127.0.0.1:{httpd.server_port}/"]).start()
    return httpd


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Skill Plus Plus</title>
<style>
 :root{--bg:#fbfbfa;--fg:#1a1a18;--dim:#6b6b66;--line:#e3e3df;--card:#fff;
       --warn:#8a5a00;--warnbg:#fff6e0;--ok:#1c5f3a;--okbg:#e8f5ee}
 @media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e8e4;--dim:#96968f;
   --line:#2c2c32;--card:#1d1d22;--warn:#e0b060;--warnbg:#332a14;--ok:#7fd0a0;--okbg:#16301f}}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.55 ui-sans-serif,-apple-system,Segoe UI,sans-serif;
      background:var(--bg);color:var(--fg)}
 header{padding:22px 28px 0}
 h1{font-size:19px;margin:0 0 2px;font-weight:600}
 .sub{color:var(--dim);font-size:13px}
 nav{display:flex;gap:4px;padding:16px 28px 0;border-bottom:1px solid var(--line);
     flex-wrap:wrap}
 nav button{background:none;border:0;border-bottom:2px solid transparent;
   padding:8px 12px;font:inherit;color:var(--dim);cursor:pointer}
 nav button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--fg)}
 main{padding:20px 28px 60px;max-width:1100px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;
   padding:14px 16px;margin:0 0 10px}
 .row{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
 .title{font-weight:600}
 .desc{color:var(--dim);font-size:13.5px;margin:4px 0 0}
 .meta{color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums}
 .tag{font-size:11.5px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);
   color:var(--dim)}
 .tag.method{background:var(--okbg);color:var(--ok);border-color:transparent}
 .tag.oneoff{opacity:.75}
 .tag.warn{background:var(--warnbg);color:var(--warn);border-color:transparent}
 pre{margin:10px 0 0;padding:10px 12px;background:var(--bg);border:1px solid var(--line);
   border-radius:6px;overflow-x:auto;font:12.5px/1.6 ui-monospace,SFMono-Regular,monospace}
 textarea{width:100%;min-height:340px;font:12.5px/1.6 ui-monospace,monospace;
   background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:10px}
 button.act{font:inherit;padding:5px 11px;border:1px solid var(--line);border-radius:6px;
   background:var(--card);color:var(--fg);cursor:pointer}
 .empty{color:var(--dim);padding:26px 0}
 input[type=search]{font:inherit;padding:6px 10px;border:1px solid var(--line);
   border-radius:6px;background:var(--card);color:var(--fg);min-width:220px}
 .note{color:var(--dim);font-size:12.5px;margin:0 0 14px}
 .counts{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
 .rv{background:var(--card);border:1px solid var(--line);border-radius:8px;
     margin:0 0 6px}
 .rv.good{border-left:3px solid var(--ok)}
 .rv.duplicate,.rv.fragment{border-left:3px solid var(--warn)}
 .rvhead{display:flex;gap:10px;align-items:center;padding:9px 12px;cursor:pointer;
     flex-wrap:wrap}
 .rvhead .title{flex:1;min-width:220px}
 .rvbody{display:none;padding:0 12px 12px}
 .rv.open .rvbody{display:block}
 .picks{display:flex;gap:4px}
 .picks button{font:inherit;font-size:12px;padding:2px 9px;border-radius:99px;
     border:1px solid var(--line);background:none;color:var(--dim);cursor:pointer}
 .picks button[aria-pressed=true]{background:var(--fg);color:var(--bg);
     border-color:var(--fg)}
 .rvbody h4{margin:10px 0 4px;font-size:12px;color:var(--dim);font-weight:600}
 .rvbody ol{margin:0;padding-left:20px;font-size:13px}
 .rvsum{flex-basis:100%;font-size:13.5px;color:var(--fg);margin:2px 0 0}
 .rvsum.pending{color:var(--dim);font-style:italic}
 .rvbody ul{margin:0;padding-left:20px;font-size:13px}
</style>
<header>
 <h1>Skill Plus Plus</h1>
 <div class="sub" id="where"></div>
</header>
<nav id="tabs"></nav>
<main id="main">loading…</main>
<script>
const esc = s => String(s??"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let S=null, tab="review", q="", open=new Set();

const TABS = [
  ["review","Review"], ["candidates","Candidates"], ["parked","Parked"], ["skills","Skills"],
  ["promoted","Promoted"], ["accuracy","Accuracy"],
];

function hintTag(e){
  if(e.hint==="method") return '<span class="tag method">repeatable</span>';
  if(e.hint==="one-off") return '<span class="tag oneoff">probably one-off</span>';
  return '<span class="tag">not ranked</span>';
}

function card(e, extra=""){
  const steps = e.steps.length
    ? `<pre>${e.steps.map(esc).join("\\n")}${
        e.step_count>e.steps.length?`\\n… +${e.step_count-e.steps.length} more`:""}</pre>` : "";
  return `<div class="card">
    <div class="row">
      <span class="title">${esc(e.title)||"(untitled)"}</span>
      ${hintTag(e)}
      ${e.ready?'<span class="tag method">ready</span>':""}
      ${e.parking_looks_wrong?`<span class="tag warn">done ${e.since_parked}× since</span>`:""}
    </div>
    ${e.description?`<p class="desc">${esc(e.description)}</p>`
      :'<p class="desc"><em>no description — it decides whether a skill ever loads</em></p>'}
    <div class="meta">${e.id} · ×${e.occurrences} · ${e.step_count} steps · ${
      esc(e.source)} · ${esc(e.last_seen)}${
      e.deps.length?" · "+e.deps.map(esc).join(", "):""}</div>
    ${steps}${extra}</div>`;
}

function matches(e){
  if(!q) return true;
  const hay = (e.title+" "+e.description+" "+e.steps.join(" ")+" "+e.id).toLowerCase();
  return hay.includes(q.toLowerCase());
}

const PICKS = [["good","good"],["duplicate","duplicate"],["fragment","fragment"]];

function reviewRow(r){
  const near = r.closest
    ? `closest: ${esc(r.closest.title)} <b>${r.closest.score.toFixed(2)}</b>` : "";
  const prompts = r.intents.length
    ? `<ol>${r.intents.map(i=>`<li>${esc(i)}</li>`).join("")}</ol>`
    : '<p class="desc"><em>no prompts recorded</em></p>';
  const steps = `<ol>${r.steps.map(x=>`<li>${esc(x)}</li>`).join("")}</ol>${
    r.step_count>r.steps.length?`<p class="meta">… +${r.step_count-r.steps.length} more</p>`:""}`;
  return `<div class="rv ${esc(r.tag)} ${open.has(r.id)?"open":""}" data-id="${esc(r.id)}">
    <div class="rvhead" data-toggle="${esc(r.id)}">
      <span class="title">${esc(r.title)||"(untitled)"}</span>
      ${r.title_from_prompt?'<span class="tag warn">title is a prompt</span>':""}
      <span class="tag" title="how many separate sessions did this same work">seen in ${r.occurrences} session${r.occurrences===1?"":"s"}</span>
      <span class="tag">${r.step_count} steps</span>
      ${r.ready?'<span class="tag method">ready</span>':""}
      <span class="picks">${PICKS.map(([k,l])=>
        `<button data-tag="${k}" data-for="${esc(r.id)}" aria-pressed="${r.tag===k}">${l}</button>`
      ).join("")}</span>
      <p class="rvsum ${r.summary?"":"pending"}" data-sum="${esc(r.id)}">${
        r.summary ? esc(r.summary) : "summarising…"}</p>
    </div>
    <div class="rvbody">
      <div class="meta">${esc(r.id)} · last seen ${esc(r.last_seen)}${near?" · "+near:""}</div>
      <h4>What was asked (${r.intents.length})</h4>${prompts}
      <h4>What the assistant reported (${r.reports.length})</h4>${
        r.reports.length ? `<ul>${r.reports.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`
        : '<p class="desc"><em>no completion reports recorded</em></p>'}
      <h4>What was done (${r.step_count})</h4>${steps}
    </div></div>`;
}

function renderReview(m){
  const rows = S.review.filter(r=>!q ||
    (r.title+" "+r.intents.join(" ")+" "+r.steps.join(" ")).toLowerCase()
      .includes(q.toLowerCase()));
  const n = k => S.review.filter(r=>r.tag===k).length;
  const done = S.review.filter(r=>r.tag).length;
  m.innerHTML = `<p class="note">Judge what detection produced. Tags are stored in
      <code>review.json</code> and never change the ledger. Click a row to see
      every prompt and step.</p>
    <div class="counts">
      <span class="tag method">good ${n("good")}</span>
      <span class="tag warn">duplicate ${n("duplicate")}</span>
      <span class="tag warn">fragment ${n("fragment")}</span>
      <span class="tag">unreviewed ${S.review.length-done}</span>
    </div>
    <p><input type="search" id="q" placeholder="filter…" value="${esc(q)}"></p>` +
    (rows.length ? rows.map(reviewRow).join("") : '<p class="empty">No candidates.</p>');

  m.querySelectorAll("[data-toggle]").forEach(h=>h.onclick=ev=>{
    if(ev.target.closest(".picks")) return;
    const id=h.dataset.toggle;
    open.has(id)?open.delete(id):open.add(id);
    h.parentElement.classList.toggle("open");
  });
  m.querySelectorAll("[data-tag]").forEach(b=>b.onclick=async()=>{
    const id=b.dataset.for, row=S.review.find(r=>r.id===id);
    const tag = row.tag===b.dataset.tag ? "" : b.dataset.tag;   // click again to clear
    const r=await (await fetch("/api/review",{method:"POST",
      body:JSON.stringify({id,tag})})).json();
    if(r.ok){ row.tag=tag; render(); }
  });
  fillSummaries();
}

let filling = false;
async function fillSummaries(){
  // One at a time: each is a local model call, and the page stays usable while
  // they arrive. Rows are updated in place so an expanded row stays expanded.
  if(filling) return;
  filling = true;
  try {
    for(const r of S.review){
      if(r.summary || r.summary_failed) continue;
      let res;
      try {
        res = await (await fetch("/api/review/summary",{method:"POST",
          body:JSON.stringify({id:r.id})})).json();
      } catch(e){ res = {ok:false, error:String(e)}; }
      const el = document.querySelector(`[data-sum="${CSS.escape(r.id)}"]`);
      if(res.ok){
        r.summary = res.summary;
        if(el){ el.textContent = res.summary; el.classList.remove("pending"); }
      } else {
        r.summary_failed = true;
        if(el){ el.textContent = "no summary — " + res.error; }
        if(/no local model/.test(res.error||"")) break;   // do not hammer a dead model
      }
    }
  } finally { filling = false; }
}

function render(){
  document.getElementById("where").textContent =
    `${S.skills_dir}  ·  ledger ${S.root}  ·  threshold ×${S.threshold}`;
  const nav = document.getElementById("tabs");
  nav.innerHTML = TABS.map(([k,label])=>{
    const n = k==="accuracy" ? "" : ` (${(S[k]||[]).length})`;  // review is a list too
    return `<button role="tab" aria-selected="${k===tab}" data-tab="${k}">${label}${n}</button>`;
  }).join("");
  nav.querySelectorAll("button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;render()});

  const m = document.getElementById("main");
  const search = `<p><input type="search" id="q" placeholder="filter…" value="${esc(q)}"></p>`;

  if(tab==="review"){ renderReview(m); bindSearch(); return; }

  if(tab==="accuracy"){
    const a=S.accuracy, d=S.drift;
    m.innerHTML = `<p class="note">Scored against decisions you actually made —
      not against fixtures. Every promote and dismiss adds a label.</p>
      <div class="card"><div class="row"><span class="title">Ranker agreement</span></div>
      <div class="meta">${a.judged} decision(s)${
        a.scored?` · agreed ${a.agreed}/${a.scored}`:""}${
        a.unranked?` · ${a.unranked} decided before sift ran`:""}</div>
      ${a.misses.length?`<pre>${a.misses.map(x=>
        esc(`ranker said ${x.hint} · you ${x.decision} · ${x.title}`)).join("\\n")}</pre>`
        :'<p class="desc">No disagreements recorded.</p>'}</div>
      <div class="card"><div class="row"><span class="title">Drift</span></div>
      <div class="meta">${d.promoted} promoted · ${d.live} live${
        d.missing.length?` · ${d.missing.length} missing`:""}</div>
      ${d.missing.length?`<pre>${d.missing.map(x=>esc(x.title+"  "+x.skill_path)).join("\\n")}</pre>`
        :'<p class="desc">Every promoted skill is still on disk.</p>'}</div>`;
    return;
  }

  if(tab==="skills"){
    m.innerHTML = search + (S.skills.length?S.skills.map(s=>`<div class="card">
      <div class="row"><span class="title">${esc(s.name)}</span>
      ${s.occurrences?`<span class="tag">×${s.occurrences}</span>`:""}</div>
      <p class="desc">${esc(s.description)||"<em>no description</em>"}</p>
      <div class="meta">${s.provenance?"from "+esc(s.provenance):"no ledger entry"}</div>
      <textarea data-name="${esc(s.name)}">${esc(s.body)}</textarea>
      <p><button class="act" data-save="${esc(s.name)}">Save</button>
      <span class="meta" data-msg="${esc(s.name)}"></span></p></div>`).join("")
      : '<p class="empty">No skills installed.</p>');
    m.querySelectorAll("[data-save]").forEach(b=>b.onclick=async()=>{
      const name=b.dataset.save;
      const body=m.querySelector(`textarea[data-name="${name}"]`).value;
      const msg=m.querySelector(`[data-msg="${name}"]`);
      msg.textContent="saving…";
      const r=await (await fetch("/api/skill/save",{method:"POST",
        body:JSON.stringify({name,body})})).json();
      msg.textContent = r.ok ? `saved · backup ${r.backup}` : `refused: ${r.error}`;
    });
    return;
  }

  const rows=(S[tab]||[]).filter(matches);
  if(tab==="parked"){
    m.innerHTML = `<p class="note">Parked entries are still matched by
      recurrence — otherwise the next occurrence would rebuild the same id and
      undo the decision. So the counts keep moving, and a count that keeps
      moving is the only honest sign a parking was wrong.</p>` + search +
      (rows.length?rows.map(e=>card(e,
        `<p><button class="act" data-reopen="${esc(e.id)}">Put back in review</button></p>`
      )).join(""):'<p class="empty">Nothing parked.</p>');
    m.querySelectorAll("[data-reopen]").forEach(b=>b.onclick=async()=>{
      await fetch("/api/reopen",{method:"POST",
        body:JSON.stringify({id:b.dataset.reopen})});
      load();
    });
  } else {
    const note = tab==="candidates"
      ? `<p class="note">Ranking only — nothing here has been removed. Name and
         draft one with <code>skillpp draft &lt;id&gt; --apply</code>.</p>` : "";
    m.innerHTML = note + search + (rows.length?rows.map(e=>card(e)).join("")
      : '<p class="empty">Nothing here.</p>');
  }
  bindSearch();
}

function bindSearch(){
  const box=document.getElementById("q");
  if(box){box.oninput=()=>{q=box.value;render();
    const b=document.getElementById("q"); b.focus(); b.setSelectionRange(q.length,q.length)};}
}

async function load(){
  S = await (await fetch("/api/state")).json();
  render();
}
load();
</script>
"""
