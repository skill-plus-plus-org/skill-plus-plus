"""Tell the person when the memory guard holds or stops a fold.

The fold runs detached after the chat has ended, so a message in the session
would reach nobody, and a SessionStart hook's output goes to the model, not to
the person (Claude Code hooks reference). The operating system's own
notifications are the one place they will see it: Notification Center through
`osascript` on macOS, `notify-send` on Linux. Anywhere else, and whenever those
fail, the notice reaches the log only.

Nothing here raises. A missing notifier costs the notice, never the fold.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

TITLE = "Skill++"
# "Waiting" repeats while sessions wait, so it is sent at most this often.
# "Stopped" is sent every time: it only happens when memory ran short.
WAITING_EVERY_SECONDS = 3600


def command(message: str, platform: str | None = None) -> list[str] | None:
    """The command that shows *message* on this system, or None."""
    platform = platform or sys.platform
    if platform == "darwin":
        return ["osascript", "-e",
                f"display notification {_apple(message)} with title {_apple(TITLE)}"]
    if platform.startswith("linux") and shutil.which("notify-send"):
        return ["notify-send", "--app-name", TITLE, TITLE, message]
    return None


def _apple(text: str) -> str:
    """An AppleScript string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def send(config, message: str, *, kind: str, every: float = 0.0) -> bool:
    """Show *message*, at most once per *every* seconds for this *kind*."""
    from .capture import log_error
    from .memory import _load, _store

    log_error(config, f"notice ({kind}): {message}")
    if not config.notify:
        return False
    data = _load(config)
    last = (data.get("notified") or {}).get(kind)
    if every and isinstance(last, (int, float)) and time.time() - last < every:
        return False
    argv = command(message)
    if not argv:
        return False
    try:
        subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    data.setdefault("notified", {})[kind] = time.time()
    _store(config, data)
    return True


def waiting(config, reason: str) -> bool:
    """A fold was held: the models do not fit in memory now."""
    return send(config,
                f"Skill++ is waiting to fold your session: it {reason}. "
                "It folds on its own once your computer is idle.",
                kind="waiting", every=WAITING_EVERY_SECONDS)


def stopped(config, reason: str, freed: int | None) -> bool:
    """The watchdog stopped a fold and unloaded the models."""
    from .memory import gb
    what = f"stopped and freed {gb(freed)} GB" if freed else "stopped its local model"
    return send(config,
                f"Skill++ {what}: {reason}. "
                "Your session is kept and folded later.",
                kind="stopped")
