"""A local page for deciding what becomes a skill.

One list: the candidates seen in enough sessions to be worth a decision, and
what was decided about them. Accept promotes, Decline dismisses, and an
accepted candidate gets a Create Skill button that has the developer's agent
write a draft. Everything else the ledger holds stays in the CLI.

Every action runs an existing command — `skillpp promote`, `skillpp dismiss`,
`skillpp draft --apply` — as a subprocess, so the page cannot drift from what
the commands do and there is no second copy of their rules to keep in step.

Binds to 127.0.0.1 with no authentication; it must never be exposed.
Dependency-free on purpose: `http.server` and one self-contained page.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .ledger import (STATUS_CANDIDATE, STATUS_DISMISSED, STATUS_PROMOTED,
                     Ledger)
from .lifecycle import parse_frontmatter

CLI = Path(__file__).resolve().parent.parent / "bin" / "skillpp"
# `skillpp draft` gives the agent 900 seconds by default. A job still marked
# running well past that died with the server that started it.
DRAFT_STALE_SECONDS = 1200

_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Written by this page or by the agent's editor, never part of the skill.
_NOT_SKILL_FILES = ("status.json",)

_jobs: dict[str, threading.Thread] = {}
_jobs_lock = threading.Lock()


def _run(config: Config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), "--root", str(config.root),
                           *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def _draft_dir(config: Config, entry_id: str) -> Path:
    return config.root / "drafts" / entry_id


def _status_path(config: Config, entry_id: str) -> Path:
    return _draft_dir(config, entry_id) / "status.json"


def _write_status(config: Config, entry_id: str, **status) -> None:
    path = _status_path(config, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    tmp.replace(path)


def _read_status(config: Config, entry_id: str) -> dict:
    try:
        return json.loads(_status_path(config, entry_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tail(text: str, lines: int = 4) -> str:
    kept = [line for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def row_state(config: Config, entry) -> dict:
    """What a row shows, read from the ledger and the draft directory only."""
    if entry.status == STATUS_DISMISSED:
        # Not "declined": that is the agent refusing to draft, shown with a retry.
        return {"state": "dismissed"}
    if entry.status != STATUS_PROMOTED:
        return {"state": "undecided"}
    if entry.skill_path and Path(entry.skill_path).expanduser().exists():
        return {"state": "installed", "path": entry.skill_path}
    status = _read_status(config, entry.id)
    if status.get("state") == "running":
        if time.time() - status.get("started", 0) < DRAFT_STALE_SECONDS:
            return {"state": "creating"}
        return {"state": "failed", "message": "the draft run did not finish"}
    drafted = sorted(_draft_dir(config, entry.id).rglob("SKILL.md"))
    if drafted:
        return {"state": "drafted", "path": str(drafted[0])}
    if status.get("state") in ("failed", "declined"):
        return {"state": status["state"], "message": status.get("message", "")}
    return {"state": "accepted"}


def collect_state(config: Config) -> dict:
    """The rows the page lists. Reads files only: no model, no agent."""
    threshold = config.recurrence_threshold
    rows = []
    for entry in Ledger(config).all():
        listed = ((entry.status == STATUS_CANDIDATE and entry.ready(threshold)
                   and entry.source == "capture")
                  or entry.status in (STATUS_PROMOTED, STATUS_DISMISSED))
        if not listed:
            continue
        rows.append({"id": entry.id, "title": entry.title,
                     "occurrences": entry.occurrences, **row_state(config, entry)})
    rows.sort(key=lambda r: (-r["occurrences"], (r["title"] or "").lower()))
    return {"threshold": threshold, "rows": rows, "drafts": list_drafts(config)}


def _skill_name(entry, skill_md: Path) -> str:
    """The folder name the skill unpacks to: its frontmatter name, if safe."""
    name = str(parse_frontmatter(skill_md.read_text(encoding="utf-8")).get("name") or "")
    return name if _SAFE_NAME.match(name) else entry.id


def _draft_files(config: Config, entry_id: str) -> list[Path]:
    root = _draft_dir(config, entry_id)
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.name not in _NOT_SKILL_FILES
                  and not p.name.endswith(".tmp"))


def list_drafts(config: Config) -> list[dict]:
    """Every finished draft, with the SKILL.md text to review."""
    drafts = []
    for entry in Ledger(config).all():
        if row_state(config, entry)["state"] != "drafted":
            continue
        skill_md = sorted(_draft_dir(config, entry.id).rglob("SKILL.md"))[0]
        text = skill_md.read_text(encoding="utf-8")
        front = parse_frontmatter(text)
        drafts.append({
            "id": entry.id, "title": entry.title,
            "name": _skill_name(entry, skill_md),
            "description": str(front.get("description") or ""),
            "body": text,
            "files": [str(p.relative_to(_draft_dir(config, entry.id)))
                      for p in _draft_files(config, entry.id)],
        })
    drafts.sort(key=lambda d: d["name"].lower())
    return drafts


def draft_zip(config: Config, entry_id: str) -> tuple[str, bytes] | None:
    """A draft as `<skill-name>.zip`, holding `<skill-name>/SKILL.md` and any
    files beside it, so it unpacks straight into a skills directory.

    Only a ledger entry with a finished draft is served; the id is looked up,
    never joined into a path from the request.
    """
    entry = Ledger(config).get(entry_id)
    if not entry or row_state(config, entry)["state"] != "drafted":
        return None
    root = _draft_dir(config, entry.id)
    name = _skill_name(entry, sorted(root.rglob("SKILL.md"))[0])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in _draft_files(config, entry.id):
            archive.write(path, f"{name}/{path.relative_to(root)}")
    return f"{name}.zip", buffer.getvalue()


def _decide(config: Config, entry_id: str, command: str) -> dict:
    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    if row_state(config, entry)["state"] != "undecided":
        return {"ok": False, "error": f"{entry.id} is already {entry.status}"}
    proc = _run(config, command, entry.id)
    if proc.returncode != 0:
        return {"ok": False, "error": _tail(proc.stderr or proc.stdout)}
    return {"ok": True}


def accept(config: Config, entry_id: str) -> dict:
    """Accept Skill: `skillpp promote <id>`, with no skill file yet."""
    return _decide(config, entry_id, "promote")


def decline(config: Config, entry_id: str) -> dict:
    """Decline Skill: `skillpp dismiss <id>`."""
    return _decide(config, entry_id, "dismiss")


def _draft_job(config: Config, entry_id: str) -> None:
    try:
        proc = _run(config, "draft", entry_id, "--apply")
        said = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            _write_status(config, entry_id, state="failed", message=_tail(said))
        elif not sorted(_draft_dir(config, entry_id).rglob("SKILL.md")):
            # `skillpp draft` exits 0 without a file only for a stated decline.
            _write_status(config, entry_id, state="declined", message=_tail(said, 1))
        else:
            _write_status(config, entry_id, state="ready")
    except Exception as exc:  # noqa: BLE001 - the thread must record, not raise
        _write_status(config, entry_id, state="failed",
                      message=f"{type(exc).__name__}: {exc}")
    finally:
        with _jobs_lock:
            _jobs.pop(entry_id, None)


def create_skill(config: Config, entry_id: str) -> dict:
    """Create Skill: `skillpp draft <id> --apply`, in the background.

    Only for an accepted candidate, and one run at a time per candidate. The
    draft lands in `<root>/drafts/<id>/` and is never installed from here.
    """
    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    with _jobs_lock:
        state = row_state(config, entry)["state"]
        if entry.id in _jobs or state == "creating":
            return {"ok": False, "error": "already running"}
        if state not in ("accepted", "failed", "declined"):
            return {"ok": False,
                    "error": f"cannot create a skill for a row that is {state}"}
        _write_status(config, entry.id, state="running", started=time.time())
        job = threading.Thread(target=_draft_job, args=(config, entry.id),
                               daemon=True)
        _jobs[entry.id] = job
    job.start()
    return {"ok": True}


def make_handler(config: Config):
    actions = {"/api/accept": accept, "/api/decline": decline,
               "/api/create": create_skill}

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
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._send(200, json.dumps(collect_state(config)))
            if url.path == "/api/draft.zip":
                found = draft_zip(config, (parse_qs(url.query).get("id") or [""])[0])
                if not found:
                    return self._send(404, json.dumps({"error": "no such draft"}))
                filename, data = found
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad json"}))
            action = actions.get(self.path)
            if not action:
                return self._send(404, json.dumps({"error": "not found"}))
            self._send(200, json.dumps(action(config, str(payload.get("id", "")))))

    return Handler


def serve(config: Config, skills_dir: Path | None = None, port: int = 8765,
          open_browser: bool = True) -> ThreadingHTTPServer:
    """Serve on loopback only. Never bind anywhere else: there is no auth."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(config))
    if open_browser:
        threading.Thread(target=webbrowser.open, daemon=True,
                         args=[f"http://127.0.0.1:{httpd.server_port}/"]).start()
    return httpd


# Raw: the page script holds regular expressions, and their backslashes must
# reach the browser unchanged.
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>skillpp</title>
<style>
 :root{--bg:#0c0d10;--panel:#12141a;--surface:#171922;--line:#262935;
   --fg:#eceef2;--dim:#9da3b4;--muted:#63697a;
   --ok:#34d399;--okbg:rgba(16,185,129,.12);--okline:rgba(16,185,129,.4);
   --no:#fb7185;--nobg:rgba(244,63,94,.12);--noline:rgba(244,63,94,.4);
   --go:#38bdf8;--gobg:rgba(56,189,248,.12);--goline:rgba(56,189,248,.4);
   --sans:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;
   --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 var(--sans);
   -webkit-font-smoothing:antialiased}
 header{display:flex;justify-content:space-between;align-items:center;gap:12px;
   padding:14px 24px;border-bottom:1px solid var(--line);background:var(--panel);
   font-size:12px;color:var(--dim)}
 header b{font:600 14px var(--mono);color:var(--fg)}
 main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
 .row{display:flex;align-items:center;gap:16px;padding:14px 16px;margin-bottom:8px;
   background:var(--panel);border:1px solid var(--line);border-radius:8px}
 .title{flex:1;min-width:0;font:600 13.5px var(--mono);white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 .seen{font:12px var(--mono);color:var(--muted);white-space:nowrap}
 .acts{display:flex;align-items:center;gap:8px;flex-shrink:0;min-height:30px}
 button{font:500 12px var(--mono);padding:6px 12px;border-radius:5px;cursor:pointer;
   background:var(--surface);color:var(--dim);border:1px solid var(--line)}
 button:disabled{opacity:.5;cursor:default}
 button.accept:hover{color:var(--ok);border-color:var(--okline);background:var(--okbg)}
 button.decline:hover{color:var(--no);border-color:var(--noline);background:var(--nobg)}
 button.create{color:var(--go);border-color:var(--goline);background:var(--gobg)}
 .state{font:12px var(--mono);color:var(--dim);white-space:nowrap}
 .state.ok{color:var(--ok)} .state.no{color:var(--no)}
 .msg{font:12px var(--mono);color:var(--no);max-width:260px;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 .spin{display:inline-block;width:10px;height:10px;margin-right:6px;border-radius:50%;
   border:2px solid var(--line);border-top-color:var(--go);animation:s 1s linear infinite;
   vertical-align:-1px}
 @keyframes s{to{transform:rotate(360deg)}}
 .empty{color:var(--muted);padding:32px 0;text-align:center}
 nav{display:flex;gap:4px}
 nav button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;
   padding:4px 10px;color:var(--dim)}
 nav button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--fg)}
 a.state{text-decoration:none;cursor:pointer}
 .draft{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
 .draft .row{margin:0;border:0;background:none;cursor:pointer}
 .chev{color:var(--muted);font:12px var(--mono);width:12px;transition:transform .15s}
 .draft.open .chev{transform:rotate(90deg)}
 .draft .desc{padding:0 16px 12px 44px;color:var(--dim);font-size:12.5px;margin:0}
 .draft .body{display:none;border-top:1px solid var(--line);padding:14px 16px}
 .draft.open .body{display:block}
 .md{color:#cbd2e1;font-size:13.5px;line-height:1.6;max-width:760px}
 .md h1{font-size:18px;margin:18px 0 8px;color:var(--fg)}
 .md h2{font-size:15px;margin:20px 0 6px;color:var(--fg)}
 .md h3,.md h4,.md h5,.md h6{font-size:13.5px;margin:16px 0 4px;color:var(--fg)}
 .md p{margin:6px 0} .md li>p{margin:2px 0}
 .md ul,.md ol{margin:6px 0;padding-left:22px} .md li{margin:3px 0}
 .md code{font:12px var(--mono);background:var(--surface);border:1px solid var(--line);
   border-radius:4px;padding:1px 5px}
 .md pre{margin:8px 0;padding:12px;background:var(--bg);border:1px solid var(--line);
   border-radius:6px;overflow:auto}
 .md pre code{background:none;border:0;padding:0;white-space:pre;font-size:12px;line-height:1.55}
 .md blockquote{margin:8px 0;padding-left:12px;border-left:2px solid var(--line);color:var(--dim)}
 .md hr{border:0;border-top:1px solid var(--line);margin:16px 0}
 .md .link{text-decoration:underline dotted;color:var(--fg)}
 .md table.fm{border-collapse:collapse;margin:0 0 12px;font:11.5px var(--mono);width:100%}
 .md .fm th{text-align:left;color:var(--muted);font-weight:500;padding:3px 12px 3px 0;
   white-space:nowrap;vertical-align:top;width:1%}
 .md .fm td{color:var(--dim);padding:3px 0;word-break:break-word}
 .files{font:11.5px var(--mono);color:var(--muted);margin:0 0 10px}
 a.download{font:500 12px var(--mono);padding:6px 12px;border-radius:5px;text-decoration:none;
   color:var(--ok);border:1px solid var(--okline);background:var(--okbg);white-space:nowrap}
 @media(max-width:640px){.row{flex-wrap:wrap}.title{flex-basis:100%}}
</style></head><body>
<header><span style="display:flex;align-items:center;gap:18px"><b>skillpp</b>
<nav id="nav"></nav></span><span id="where"></span></header>
<main id="list"></main>
<script>
let S = {rows:[], drafts:[]}, busy = new Set(), timer = null;
let view = "candidates", open = new Set();
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function actions(r){
  const id = esc(r.id), off = busy.has(r.id) ? " disabled" : "";
  switch(r.state){
    case "undecided": return `<button class="accept" data-act="accept" data-id="${id}"${off}>Accept Skill</button>
      <button class="decline" data-act="decline" data-id="${id}"${off}>Decline Skill</button>`;
    case "accepted": return `<button class="create" data-act="create" data-id="${id}"${off}>Create Skill</button>`;
    case "creating": return `<span class="state"><span class="spin"></span>Creating skill…</span>`;
    case "drafted": return `<a class="state ok" data-goto="${id}" title="Review in Drafts">Draft ready →</a>`;
    case "installed": return `<span class="state ok" title="${esc(r.path)}">Skill installed</span>`;
    case "failed": case "declined":
      return `<span class="msg" title="${esc(r.message)}">${r.state==="declined" ? "Agent declined" : "Failed"}: ${esc(r.message)}</span>
        <button class="create" data-act="create" data-id="${id}"${off}>Create Skill</button>`;
    case "dismissed": return `<span class="state no">Declined</span>`;
    default: return "";
  }
}

function renderNav(){
  const nav = document.getElementById("nav");
  nav.innerHTML = [["candidates","Candidates",S.rows.length],["drafts","Drafts",S.drafts.length]]
    .map(([k,l,n]) => `<button data-view="${k}" aria-selected="${view===k}">${l} (${n})</button>`).join("");
  nav.querySelectorAll("[data-view]").forEach(b => b.onclick = () => { view = b.dataset.view; render(); });
}

// Markdown for reviewing a SKILL.md. Everything is escaped first; only the
// tags written here reach the page. Links show their text and never navigate.
function mdInline(raw){
  const codes = [];
  let t = raw.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "@@code" + (codes.length - 1) + "@@"; });
  t = esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<![*\w])\*([^*\s][^*]*?)\*(?![*\w])/g, "<em>$1</em>")
    .replace(/(?<![_\w])_([^_\s][^_]*?)_(?![_\w])/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<span class="link" title="$2">$1</span>');
  return t.replace(/@@code(\d+)@@/g, (m, i) => `<code>${esc(codes[i])}</code>`);
}

function mdFrontmatter(fm){
  const rows = [];
  let parent = "";
  for(const line of fm.split("\n")){
    const m = line.match(/^(\s*)([\w.-]+):\s*(.*)$/);
    if(!m) continue;
    const [, indent, key, value] = m;
    if(!indent && !value){ parent = key; continue; }
    if(!indent) parent = "";
    const name = indent && parent ? `${parent}.${key}` : key;
    rows.push(`<tr><th>${esc(name)}</th><td>${esc(value.replace(/^"(.*)"$/, "$1"))}</td></tr>`);
  }
  return rows.length ? `<table class="fm">${rows.join("")}</table>` : "";
}

function md(src){
  let body = src.replace(/\r\n/g, "\n"), head = "";
  const fm = body.match(/^---\n([\s\S]*?)\n---\n?/);
  if(fm){ head = mdFrontmatter(fm[1]); body = body.slice(fm[0].length); }
  const out = [], lines = body.split("\n"), lists = [];
  let para = [];
  const flush = () => { if(para.length){ out.push(`<p>${mdInline(para.join(" "))}</p>`); para = []; } };
  const closeTo = indent => {
    while(lists.length && lists[lists.length - 1].indent > indent){
      out.push(`</li></${lists.pop().type}>`);
    }
  };
  for(let i = 0; i < lines.length; i++){
    const line = lines[i], indent = line.match(/^\s*/)[0].length, text = line.trim();
    if(text.startsWith("```")){
      flush();
      if(indent === 0) closeTo(-1);
      const code = [];
      for(i++; i < lines.length && !lines[i].trim().startsWith("```"); i++){
        code.push(lines[i].slice(Math.min(indent, lines[i].match(/^\s*/)[0].length)));
      }
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    if(!text){ flush(); continue; }
    const item = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if(item){
      flush();
      const type = /\d/.test(item[2]) ? "ol" : "ul";
      closeTo(indent);
      const top = lists[lists.length - 1];
      if(top && top.indent === indent && top.type === type){
        out.push("</li><li>");
      } else {
        if(top && top.indent === indent) out.push(`</li></${lists.pop().type}>`);
        lists.push({type, indent});
        out.push(`<${type}><li>`);
      }
      para.push(item[3]);
      continue;
    }
    if(lists.length && indent > 0){ para.push(text); continue; }
    const h = text.match(/^(#{1,6})\s+(.*)$/);
    const rule = /^(-{3,}|\*{3,}|_{3,})$/.test(text);
    const q = text.match(/^>\s?(.*)$/);
    if(h || rule || q || lists.length){ flush(); closeTo(-1); }
    if(h){ const n = h[1].length; out.push(`<h${n}>${mdInline(h[2])}</h${n}>`); continue; }
    if(rule){ out.push("<hr>"); continue; }
    if(q){ out.push(`<blockquote>${mdInline(q[1])}</blockquote>`); continue; }
    para.push(text);
  }
  flush(); closeTo(-1);
  return head + out.join("\n");
}

function renderDrafts(list){
  list.innerHTML = S.drafts.length ? S.drafts.map(d => `<div class="draft ${open.has(d.id)?"open":""}">
      <div class="row" data-toggle="${esc(d.id)}">
        <span class="chev">›</span>
        <span class="title" title="${esc(d.title)}">${esc(d.name)}</span>
        <span class="seen">${esc(d.title)}</span>
        <span class="acts"><a class="download" href="/api/draft.zip?id=${encodeURIComponent(d.id)}"
          download="${esc(d.name)}.zip">Download skill</a></span>
      </div>
      <p class="desc">${esc(d.description)}</p>
      <div class="body">
        <p class="files">${d.files.map(esc).join(" · ")}</p>
        <div class="md">${md(d.body)}</div>
      </div></div>`).join("")
    : `<p class="empty">No drafts yet. Accept a candidate, then Create Skill.</p>`;
  list.querySelectorAll("[data-toggle]").forEach(h => h.onclick = ev => {
    if(ev.target.closest("a")) return;
    const id = h.dataset.toggle;
    open.has(id) ? open.delete(id) : open.add(id);
    h.parentElement.classList.toggle("open");
  });
}

function render(){
  document.getElementById("where").textContent = `threshold ≥ ${S.threshold} sessions`;
  renderNav();
  const list = document.getElementById("list");
  if(view === "drafts") return renderDrafts(list);
  list.innerHTML = S.rows.length ? S.rows.map(r => `<div class="row">
      <span class="title" title="${esc(r.title)}">${esc(r.title) || "(untitled)"}</span>
      <span class="seen">seen in ${r.occurrences} session${r.occurrences===1?"":"s"}</span>
      <span class="acts">${actions(r)}</span></div>`).join("")
    : `<p class="empty">No candidates seen in ${S.threshold} or more sessions yet.</p>`;
  list.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(b.dataset.act, b.dataset.id));
  list.querySelectorAll("[data-goto]").forEach(a => a.onclick = () => {
    view = "drafts"; open.add(a.dataset.goto); render();
  });
}

async function act(what, id){
  busy.add(id); render();
  try {
    const r = await (await fetch("/api/" + what, {method:"POST", body: JSON.stringify({id})})).json();
    if(!r.ok) alert(r.error || "failed");
  } finally { busy.delete(id); await load(); }
}

async function load(){
  S = await (await fetch("/api/state")).json();
  render();
  clearTimeout(timer);
  if(S.rows.some(r => r.state === "creating")) timer = setTimeout(load, 5000);
}
load();
</script>
</body></html>
"""
