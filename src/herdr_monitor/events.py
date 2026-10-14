"""Bounded events, private rotating audit, and a target-scoped writer lock."""

from collections import deque
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid


ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def clean(value: object, limit: int = 500) -> str:
    text = ESCAPES.sub("", str(value))
    return "".join(c for c in text if c.isprintable() or c == " ")[:limit]


@dataclass(frozen=True)
class Event:
    seq: int
    timestamp: float
    kind: str
    pane_id: str
    summary: str


class EventJournal:
    def __init__(self, path: Path, target: str, *, max_events: int = 1000,
                 max_bytes: int = 5 * 1024 * 1024, backups: int = 3):
        self.path = path
        self.target = target
        self.run_id = uuid.uuid4().hex[:12]
        self.events: deque[Event] = deque(maxlen=max_events)
        self.seq = 0
        self.failed = ""
        self.max_bytes = max_bytes
        self.backups = backups
        self._fd: int | None = None

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return fd

    def _rotate(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
            dest = Path(f"{self.path}.{index}")
            if source.exists():
                os.replace(source, dest)

    def append(self, kind: str, pane_id: str = "", summary: str = "", *, durable: bool = False) -> bool:
        self.seq += 1
        event = Event(self.seq, time.time(), clean(kind, 40), clean(pane_id, 100), clean(summary))
        self.events.append(event)
        try:
            if self.failed:
                return False
            if self._fd is None:
                self._fd = self._open()
            if os.fstat(self._fd).st_size >= self.max_bytes:
                self._rotate()
                self._fd = self._open()
            payload = {**asdict(event), "run_id": self.run_id, "target": self.target}
            encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
            if os.write(self._fd, encoded) != len(encoded):
                raise OSError("short audit write")
            if durable:
                os.fsync(self._fd)
            return True
        except OSError as exc:
            self.failed = clean(f"audit unavailable: {exc}")
            return False

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class WriterLock:
    def __init__(self, target: str, directory: Path | None = None):
        directory = directory or Path.home() / ".cache" / "herdr-monitor"
        digest = hashlib.sha256(target.encode()).hexdigest()[:24]
        self.path = directory / f"writer-{digest}.lock"
        self.fd: int | None = None

    def acquire(self) -> bool:
        if self.fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
