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

import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .ledger import (STATUS_CANDIDATE, STATUS_DISMISSED, STATUS_PROMOTED,
                     Ledger)

CLI = Path(__file__).resolve().parent.parent / "bin" / "skillpp"
# `skillpp draft` gives the agent 900 seconds by default. A job still marked
# running well past that died with the server that started it.
DRAFT_STALE_SECONDS = 1200

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
    return {"threshold": threshold, "rows": rows}


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
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps(collect_state(config)))
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


PAGE = """<!doctype html>
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
 @media(max-width:640px){.row{flex-wrap:wrap}.title{flex-basis:100%}}
</style></head><body>
<header><b>skillpp</b><span id="where"></span></header>
<main id="list"></main>
<script>
let S = {rows:[]}, busy = new Set(), timer = null;
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function actions(r){
  const id = esc(r.id), off = busy.has(r.id) ? " disabled" : "";
  switch(r.state){
    case "undecided": return `<button class="accept" data-act="accept" data-id="${id}"${off}>Accept Skill</button>
      <button class="decline" data-act="decline" data-id="${id}"${off}>Decline Skill</button>`;
    case "accepted": return `<button class="create" data-act="create" data-id="${id}"${off}>Create Skill</button>`;
    case "creating": return `<span class="state"><span class="spin"></span>Creating skill…</span>`;
    case "drafted": return `<span class="state ok" title="${esc(r.path)}">Draft ready</span>`;
    case "installed": return `<span class="state ok" title="${esc(r.path)}">Skill installed</span>`;
    case "failed": case "declined":
      return `<span class="msg" title="${esc(r.message)}">${r.state==="declined" ? "Agent declined" : "Failed"}: ${esc(r.message)}</span>
        <button class="create" data-act="create" data-id="${id}"${off}>Create Skill</button>`;
    case "dismissed": return `<span class="state no">Declined</span>`;
    default: return "";
  }
}

function render(){
  document.getElementById("where").textContent = `threshold ≥ ${S.threshold} sessions`;
  const list = document.getElementById("list");
  list.innerHTML = S.rows.length ? S.rows.map(r => `<div class="row">
      <span class="title" title="${esc(r.title)}">${esc(r.title) || "(untitled)"}</span>
      <span class="seen">seen in ${r.occurrences} session${r.occurrences===1?"":"s"}</span>
      <span class="acts">${actions(r)}</span></div>`).join("")
    : `<p class="empty">No candidates seen in ${S.threshold} or more sessions yet.</p>`;
  list.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(b.dataset.act, b.dataset.id));
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
