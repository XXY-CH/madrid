"""Fast file-change detection for chat.db (kqueue, stdlib only).

macOS doesn't give third-party processes a push channel for new iMessages,
but `chat.db-wal` changes the instant Messages.app writes a row. Watching
that file with kqueue turns the 2s polling loop into sub-second delivery —
the same effective latency as imsg's FSEvents approach, without leaving the
standard library.

Usage (inside watch.Watcher):

    from madrid import fastwatch
    fastwatch.wait_for_change(paths, timeout=2.0)   # blocks until WAL grows

Or run the kqueue in a background thread and fire a callback:

    fastwatch.start(paths, callback, stop_event)
"""
from __future__ import annotations

import os
import select
import threading
from typing import Callable


class FastWatch:
    """Watch files for size/mtime changes via kqueue (macOS/BSD)."""

    def __init__(self, paths: list[str]):
        self.paths = [p for p in paths if os.path.exists(p)]
        self._kq = None
        self._fds: list[int] = []

    def _setup(self) -> None:
        self._kq = select.kqueue()
        for p in self.paths:
            fd = os.open(p, os.O_RDONLY)
            self._fds.append(fd)
            # NOTE_WRITE: file appended/written
            # NOTE_EXTEND: file extended (WAL grows)
            # NOTE_ATTRIB: metadata changed (ctime bump)
            ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                               | select.KQ_NOTE_ATTRIB)
            self._kq.control([ev], 0)

    def _teardown(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
        if self._kq:
            try:
                self._kq.close()
            except OSError:
                pass
        self._kq = None

    def wait(self, timeout: float = 2.0) -> bool:
        """Block until any watched file changes. Returns True on change,
        False on timeout. Re-arms automatically."""
        if self._kq is None:
            self._setup()
        try:
            events = self._kq.control(None, 1, timeout)
            return len(events) > 0
        except OSError:
            # file rotated/deleted; re-setup
            self._teardown()
            try:
                self._setup()
            except OSError:
                pass
            return False

    def close(self) -> None:
        self._teardown()

    # -- background thread helper -----------------------------------------

    def start_bg(self, callback: Callable[[], None],
                 stop: threading.Event,
                 poll_interval: float = 0.1) -> None:
        """Run kqueue in a daemon thread; fire callback on change.

        Falls back to tight stat-polling if kqueue is unavailable
        (non-macOS or file access denied).
        """
        def _run():
            while not stop.is_set():
                if self._kq is not None:
                    changed = self.wait(timeout=poll_interval)
                else:
                    # stat-based fallback: watch file sizes
                    changed = self._stat_wait(poll_interval)
                if changed:
                    try:
                        callback()
                    except Exception:
                        pass

        threading.Thread(target=_run, name="madrid-fastwatch", daemon=True).start()

    def _stat_wait(self, interval: float) -> bool:
        """Fallback: compare file sizes on a tight loop."""
        if not hasattr(self, "_last_sizes"):
            self._last_sizes = {p: os.path.getsize(p) for p in self.paths
                                if os.path.exists(p)}
            import time
            time.sleep(interval)
            return False
        import time
        time.sleep(interval)
        for p in self.paths:
            if not os.path.exists(p):
                continue
            try:
                sz = os.path.getsize(p)
                if sz != self._last_sizes.get(p):
                    self._last_sizes[p] = sz
                    return True
            except OSError:
                pass
        return False


def watch_sources(db_path: str) -> list[str]:
    """Files whose change indicates new iMessage data."""
    return [db_path + "-wal", db_path + "-shm", db_path]
