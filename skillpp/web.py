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
let S=null, tab="candidates", q="";

const TABS = [
  ["candidates","Candidates"], ["parked","Parked"], ["skills","Skills"],
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

function render(){
  document.getElementById("where").textContent =
    `${S.skills_dir}  ·  ledger ${S.root}  ·  threshold ×${S.threshold}`;
  const nav = document.getElementById("tabs");
  nav.innerHTML = TABS.map(([k,label])=>{
    const n = k==="accuracy" ? "" : ` (${(S[k]||[]).length})`;
    return `<button role="tab" aria-selected="${k===tab}" data-tab="${k}">${label}${n}</button>`;
  }).join("");
  nav.querySelectorAll("button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;render()});

  const m = document.getElementById("main");
  const search = `<p><input type="search" id="q" placeholder="filter…" value="${esc(q)}"></p>`;

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
  const box=document.getElementById("q");
  if(box){box.oninput=()=>{q=box.value;render();box.focus()};}
}

async function load(){
  S = await (await fetch("/api/state")).json();
  render();
}
load();
</script>
"""
