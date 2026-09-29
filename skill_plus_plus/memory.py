"""Keep the local model from pushing the machine into swap.

The local model is the one large thing Skill++ runs. Measured on an 18 GB Mac
(docs/usage.md, "Memory"): loading gemma4:e4b and nomic-embed-text took 12.8
GB of available memory (a fold with gemma4:e4b-it-qat, the default after it,
about 7), and a fold with other apps open sat at 92 % used, macOS pressure at
warning, with 4.7 GB swapped out in 100 seconds. Nothing leaked: a
one-word question locks as much as a whole session. So the fix is not in how a
session is read but in when the models may load, and how long they stay.

Three rules, in the order they act:

* **Before loading.** A model that is not loaded yet loads only if what the
  models need still leaves `config.memory_reserve_gb` free. Otherwise nothing
  loads and the caller holds the session. Folding later costs nothing.
* **While folding.** A watchdog thread reads available memory twice a second.
  Below the reserve, or at critical pressure, it trips: no further model call
  starts, and the models this guard loaded are unloaded at once.
* **After folding.** Whatever this guard loaded is unloaded when the guard is
  released, not five minutes later when Ollama's own `keep_alive` would.

What the models need is measured on each computer: estimated from their size
on disk the first time, then the largest drop in available memory a fold has
seen here. Only models this guard loaded are ever unloaded; a model someone else
had loaded stays loaded.

Every reader answers `None` when it cannot tell, and an unknown reading turns
the guard off rather than holding every session forever.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

GB = 1024 ** 3

# Size on disk times this is the first guess at what loading takes. Measured in
# bytes on the calibration run: 9.88e9 on disk (gemma4:e4b and nomic-embed-text,
# as Ollama lists them) took 13.7e9 of available memory, which is 12.8 GiB: the
# weights plus the buffers and cache that loading brings along. It guesses high
# for gemma4:e4b-it-qat: 6.42e9 on disk, 8.3 GiB guessed, 7.0 GiB measured. The
# guess only decides the first fold of a model set on a computer; from then on
# the measured figure does (`need_bytes`).
NEED_FACTOR = 1.39
# How long `unload` waits for Ollama to stop listing a model. It answers the
# unload at once and lets go a moment later; a check made in between counted a
# model that was leaving as loaded by someone else, and never unloaded it again.
_UNLOAD_WAIT_SECONDS = 15.0
# After the watchdog trips, how long it keeps watching for a model that was
# still loading. Measured: a trip 4.3 s into a load found nothing listed to
# unload, and the load finished anyway, 9.3 GB that stayed for `KEEP_ALIVE`.
# A load of gemma4:e4b takes 10-14 s, so 20 s sees it land and unloads it.
_LINGER_SECONDS = 20.0
# A drop larger than this multiple of the estimate is something else growing at
# the same time, a browser or a build, not the models. It is not stored.
_OUTLIER = 2.0
# How often the watchdog reads memory. A load moves gigabytes in seconds.
WATCH_SECONDS = 0.5
# macOS `kern.memorystatus_vm_pressure_level`: 1 normal, 2 warning, 4 critical.
_CRITICAL = 4
# Linux PSI: every task stalled on memory for this share of the last ten
# seconds is a machine that is thrashing.
_PSI_FULL_CRITICAL = 10.0
# How long a model this guard loaded stays after its last call if the process
# dies before `release()`. Ollama's own default is five minutes.
KEEP_ALIVE = "60s"
# How long after an unload the freed memory is read, for the notice.
_SETTLE_SECONDS = 1.0


# -- readers ----------------------------------------------------------------

def _run(argv: list[str], timeout: float = 5.0) -> str | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _sysctl(name: str) -> int | None:
    out = _run(["sysctl", "-n", name])
    try:
        return int(out.strip()) if out else None
    except ValueError:
        return None


def _meminfo() -> dict[str, int]:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def total_bytes() -> int | None:
    if sys.platform == "darwin":
        return _sysctl("hw.memsize")
    if sys.platform.startswith("linux"):
        return _meminfo().get("MemTotal")
    return None


def available_bytes() -> int | None:
    """Memory the system can hand out without swapping. Not "free".

    Free memory sits near zero on a Mac by design, because spare RAM holds
    cache, and a check against it would never let a fold start: 0.3 GB free
    while the kernel reported 81 % available. macOS says what it can hand out as
    a percentage (`kern.memorystatus_level`); Linux as `MemAvailable`.
    """
    if sys.platform == "darwin":
        level, total = _sysctl("kern.memorystatus_level"), _sysctl("hw.memsize")
        return total * level // 100 if level is not None and total else None
    if sys.platform.startswith("linux"):
        return _meminfo().get("MemAvailable")
    return None


def pressure_critical() -> bool:
    """Has the system itself declared a memory emergency?"""
    if sys.platform == "darwin":
        return _sysctl("kern.memorystatus_vm_pressure_level") == _CRITICAL
    if sys.platform.startswith("linux"):
        try:
            text = Path("/proc/pressure/memory").read_text(encoding="utf-8")
        except OSError:
            return False
        m = re.search(r"^full avg10=([\d.]+)", text, re.M)
        return bool(m) and float(m.group(1)) >= _PSI_FULL_CRITICAL
    return False


def idle_seconds() -> float | None:
    """How long nobody has used the keyboard or mouse, or None if unknown."""
    if sys.platform == "darwin":
        return mac_idle(_run(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    if sys.platform.startswith("linux"):
        session = os.environ.get("XDG_SESSION_ID")
        if session:
            idle = loginctl_idle(_run(["loginctl", "show-session", session,
                                       "-p", "IdleHint", "-p", "IdleSinceHint"]))
            if idle is not None:
                return idle
        return xprintidle_idle(_run(["xprintidle"]))
    return None


def mac_idle(text: str | None) -> float | None:
    """`ioreg`'s `HIDIdleTime`, in nanoseconds, as seconds."""
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text or "")
    return int(m.group(1)) / 1e9 if m else None


def loginctl_idle(text: str | None, now: float | None = None) -> float | None:
    """Seconds idle from `loginctl show-session`, or None if it does not say.

    `IdleSinceHint` is wall-clock microseconds. A desktop that never sets the
    hint reports `IdleHint=no` forever; that reads as "not idle", which only
    delays a fold, never forces one.
    """
    values = dict(line.split("=", 1) for line in (text or "").splitlines() if "=" in line)
    if "IdleHint" not in values:
        return None
    if values["IdleHint"].strip() != "yes":
        return 0.0
    try:
        since = int(values.get("IdleSinceHint", "0"))
    except ValueError:
        return None
    if not since:
        return None
    return max(0.0, (time.time() if now is None else now) - since / 1e6)


def xprintidle_idle(text: str | None) -> float | None:
    """`xprintidle` prints milliseconds."""
    try:
        return int((text or "").strip()) / 1000
    except ValueError:
        return None


# -- Ollama -----------------------------------------------------------------

def _ollama(config, path: str, payload: dict | None = None,
            timeout: float = 10.0) -> dict | None:
    url = f"{config.ollama_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def model_name(name: str) -> str:
    """`nomic-embed-text` and `nomic-embed-text:latest` are one model.

    Ollama lists the tag; the configuration usually leaves it off.
    """
    name = str(name or "").strip()
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def loaded_models(config) -> set[str] | None:
    """What Ollama holds in memory now, or None if it cannot be asked."""
    reply = _ollama(config, "/api/ps")
    if reply is None:
        return None
    return {model_name(m.get("name") or m.get("model") or "")
            for m in reply.get("models") or []}


def model_sizes(config) -> dict[str, int]:
    """Each installed model's size on disk, in bytes."""
    reply = _ollama(config, "/api/tags") or {}
    return {model_name(m.get("name") or m.get("model") or ""): int(m.get("size") or 0)
            for m in reply.get("models") or []}


def unload(config, models: Iterable[str], *, linger: float = 0.0) -> None:
    """Drop *models* from memory, and return once Ollama has let them go.

    The documented way: `keep_alive` 0. `/api/generate` does it for an embedding
    model too; it answers with `done_reason: "unload"` and generates nothing.
    It answers before the model is gone, though, so this waits until `/api/ps`
    stops listing it (see `_UNLOAD_WAIT_SECONDS`).

    *linger* keeps watching that long even once nothing is listed, and unloads
    again whatever appears: a model still loading is not listed yet, ignores
    the unload, and lands a few seconds later.
    """
    names = {model_name(m) for m in models}
    if not names:
        return

    def ask(which):
        for name in sorted(which):
            _ollama(config, "/api/generate", {"model": name, "keep_alive": 0}, timeout=30.0)

    ask(names)
    asked = time.time()
    settle = asked + linger
    deadline = settle + _UNLOAD_WAIT_SECONDS
    while time.time() < deadline:
        loaded = loaded_models(config)
        if loaded is None:
            return
        listed = names & loaded
        if not listed and time.time() >= settle:
            return
        if listed and time.time() - asked >= 2.0:
            ask(listed)
            asked = time.time()
        time.sleep(0.25)


def fold_models(config) -> list[str]:
    """The models a fold may load: the judge and namer, and the embedder."""
    models = []
    if config.judge_boundaries or config.name_candidates:
        models.append(config.local_model)
    if config.match_candidates:
        models.append(config.embed_model)
    return models


# -- what the models take on this computer -----------------------------------

def _load(config) -> dict:
    try:
        data = json.loads(config.memory_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(config, data: dict) -> None:
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        tmp = config.memory_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(config.memory_file)
    except OSError:
        pass


def _key(models: Iterable[str]) -> str:
    return "+".join(sorted(model_name(m) for m in models))


def need_bytes(config, models: Iterable[str],
               sizes: dict[str, int] | None = None) -> tuple[int, bool]:
    """What loading *models* takes on this computer, and whether it was measured."""
    models = [model_name(m) for m in models]
    if not models:
        return 0, True
    stored = (_load(config).get("need", {}).get(socket.gethostname(), {})
              .get(_key(models)))
    if isinstance(stored, int) and stored > 0:
        return stored, True
    sizes = model_sizes(config) if sizes is None else sizes
    return int(sum(sizes.get(m, 0) for m in models) * NEED_FACTOR), False


def record_need(config, models: Iterable[str], drop: int, estimate: int) -> None:
    """Keep the largest drop a load of *models* caused here, within reason."""
    models = list(models)
    if not models or drop <= 0 or estimate <= 0 or drop > estimate * _OUTLIER:
        return
    data = _load(config)
    here = data.setdefault("need", {}).setdefault(socket.gethostname(), {})
    key = _key(models)
    here[key] = max(int(here.get(key) or 0), int(drop))
    _store(config, data)


def gb(n: int | None) -> str:
    return "?" if n is None else f"{n / GB:.1f}"


def shortfall(config, models: Iterable[str] | None = None) -> str:
    """Why a fold could not load its models now, or "" if it could.

    A dry run of the first rule for the idle waiter and "Fold now": it loads
    nothing and starts no watchdog.
    """
    return Guard(config).check(fold_models(config) if models is None else models)


def status(config) -> dict:
    """RAM, free memory, what a fold needs here and the free memory it starts at."""
    need, measured = need_bytes(config, fold_models(config))
    reserve = int(config.memory_reserve_gb * GB)
    return {"total": total_bytes(), "available": available_bytes(), "need": need,
            "measured": measured, "reserve": reserve, "start_at": need + reserve}


# -- the guard --------------------------------------------------------------

class Guard:
    """One fold's, sweep's or page request's use of the local models."""

    def __init__(self, config, *, unload_on_release: bool = True) -> None:
        self.config = config
        self.reserve = int(config.memory_reserve_gb * GB)
        self.unload_on_release = unload_on_release
        self.enabled = bool(config.memory_guard) and available_bytes() is not None
        self.ours: set[str] = set()        # loaded by this guard, still loaded
        self.approved: set[str] = set()    # may be called without asking again
        self.tripped = ""
        self._loaded: set[str] = set()     # everything this guard loaded
        self._before: int | None = None
        self._low: int | None = None
        self._estimate = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _missing(self, models: Iterable[str]) -> tuple[set[str], set[str]] | None:
        """The requested models not yet approved, and those of them not loaded."""
        wanted = {model_name(m) for m in models if m} - self.approved
        if not wanted:
            return set(), set()
        loaded = loaded_models(self.config)
        if loaded is None:
            # Ollama cannot be asked, so the call will fail on its own and the
            # session is held as offline. Nothing here can make that worse.
            return None
        return wanted, wanted - loaded

    def check(self, models: Iterable[str]) -> str:
        """Why *models* may not load now, or "". Changes nothing."""
        if not self.enabled:
            return ""
        found = self._missing(models)
        if not found or not found[1]:
            return ""
        return self._room(found[1], model_sizes(self.config))[0]

    def _room(self, missing: set[str], sizes: dict[str, int]) -> tuple[str, int | None]:
        need, _ = need_bytes(self.config, missing, sizes)
        available = available_bytes()
        if available is None or available - need >= self.reserve:
            return "", available
        return (f"needs {gb(need + self.reserve)} GB of free memory, "
                f"{gb(available)} GB free now"), available

    def admit(self, models: Iterable[str]) -> str:
        """Approve *models* for this guard, or say why not. Starts the watchdog."""
        if not self.enabled:
            return ""
        if self.tripped:
            return self.tripped
        found = self._missing(models)
        if found is None:
            return ""
        wanted, missing = found
        if missing:
            sizes = model_sizes(self.config)
            reason, available = self._room(missing, sizes)
            if reason:
                return reason
            with self._lock:
                if self._before is None and available is not None:
                    self._before = self._low = available
                self._estimate += int(sum(sizes.get(m, 0) for m in missing) * NEED_FACTOR)
                self.ours |= missing
                self._loaded |= missing
        self.approved |= wanted
        self._watch()
        return ""

    def before_call(self, model: str) -> None:
        """Raise `MemoryShort` unless *model* may be called now."""
        from .local import MemoryShort
        reason = self.tripped or self.admit([model])
        if reason:
            raise MemoryShort(reason)

    def owns(self, model: str) -> bool:
        return model_name(model) in self.ours

    def _watch(self) -> None:
        if self._thread is not None or not self.ours:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="skill-plus-plus-memory")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(WATCH_SECONDS):
            available = available_bytes()
            if available is None:
                continue
            with self._lock:
                self._low = available if self._low is None else min(self._low, available)
            if available < self.reserve:
                self.trip(f"free memory fell below {gb(self.reserve)} GB")
                return
            if pressure_critical():
                self.trip("memory pressure turned critical")
                return

    def trip(self, reason: str) -> None:
        """Stop: no further model call, and unload what this guard loaded, now.

        Everything it loaded, including a model still loading when this
        tripped: that one is not listed yet, so the unload lingers until it
        lands (`_LINGER_SECONDS`).
        """
        with self._lock:
            if self.tripped:
                return
            self.tripped = reason
            loaded = sorted(self._loaded)
        before = available_bytes()
        unload(self.config, loaded, linger=_LINGER_SECONDS if loaded else 0.0)
        time.sleep(_SETTLE_SECONDS)
        after = available_bytes()
        freed = after - before if before is not None and after is not None else None
        from .notify import stopped
        stopped(self.config, reason, freed if freed and freed > 0 else None)

    def release(self) -> None:
        """End of the fold: remember what the load took, and unload our models.

        A trip in progress finishes first, so a worker cannot exit while the
        watchdog is still unloading.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            loaded, before, low = set(self._loaded), self._before, self._low
            estimate = self._estimate
        if loaded and before is not None and low is not None:
            record_need(self.config, loaded, before - low, estimate)
        if loaded and (self.unload_on_release or self.tripped):
            unload(self.config, sorted(loaded))


_active: Guard | None = None
_active_lock = threading.Lock()

# One Skill++ process uses the models at a time. Two folds at once, two chats
# closed together or a session start's sweep beside a session end, each loaded
# the models for itself, and each unloaded them from under the other. Measured
# the same way: a fold that started while the one before was still unloading
# counted the leaving model as someone else's and left it loaded.
_MODELS_LOCK_SECONDS = 3600.0
_MODELS_LOCK_POLL = 2.0
BUSY = "is already folding a session; try again in a minute"


def active() -> Guard | None:
    """The guard `local.ask` and `local.embed` answer to, if one is open."""
    return _active


def _take_models_lock(path: Path, wait: bool) -> bool:
    from .capture import _acquire_lock
    deadline = time.time() + _MODELS_LOCK_SECONDS
    while True:
        if _acquire_lock(path, _MODELS_LOCK_SECONDS, "models"):
            return True
        if not wait or time.time() > deadline:
            return False
        time.sleep(_MODELS_LOCK_POLL)


@contextmanager
def guarded(config, *, wait: bool = True, **kwargs) -> Iterator[Guard]:
    """The guard for one fold, sweep or page request.

    Nested uses share the outermost: a `fold-pending` sweep keeps the models
    loaded from one session to the next and unloads them once, at the end.
    Across processes, one guard at a time holds the models: a fold waits for
    the one before it, and with *wait* off (the review page, which must not
    hang) the guard refuses every call instead.
    """
    global _active
    with _active_lock:
        outer = _active
        if outer is None:
            _active = guard = Guard(config, **kwargs)
    if outer is not None:
        yield outer
        return
    lock = config.root / "models.lock"
    held = False
    try:
        if guard.enabled:
            config.root.mkdir(parents=True, exist_ok=True)
            held = _take_models_lock(lock, wait)
            if not held:
                guard.tripped = BUSY
        yield guard
    finally:
        with _active_lock:
            _active = None
        guard.release()
        if held:
            try:
                lock.unlink()
            except OSError:
                pass
