"""Bounded asynchronous herdr CLI access; never fall back to the focused pane."""

import asyncio
from dataclasses import dataclass
import json
import math
import os
import re
import shlex
from pathlib import Path
from typing import Callable


class BackendError(Exception):
    """A failed command, invalid response, or unavailable observation."""

    def __init__(self, message: str = "", *, not_sent: bool = False):
        super().__init__(message)
        # True only when no process was spawned, so no input can have been submitted.
        self.not_sent = not_sent


class InputCancelled(BackendError):
    def __init__(self, message: str = "input cancelled before submission"):
        super().__init__(message, not_sent=True)


@dataclass
class Inventory:
    agents: list[dict]
    panes: list[dict]


@dataclass
class Evidence:
    agent: dict
    pane: dict
    rule: str
    text: str
    stable: bool


_STATES = {"idle", "working", "blocked", "done", "unknown"}
# Agents whose "blocked" comes from their own integration hook, not screen detection.
SELF_REPORTED_BLOCKED = frozenset({"pi"})
_PANE_ID = re.compile(r"w[A-Za-z0-9_-]+:p[A-Za-z0-9_-]+\Z")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MAX_OUTPUT = 2 * 1024 * 1024


def _string(value) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value)


def _pane_id(value: str) -> str:
    if not isinstance(value, str) or not _PANE_ID.fullmatch(value):
        raise BackendError("invalid pane ID")
    return value


def _integer(value) -> bool:
    return type(value) is int and value >= 0


def _metadata(item, *, agent: bool = False) -> dict:
    if not isinstance(item, dict):
        raise BackendError("invalid pane metadata")
    _pane_id(item.get("pane_id"))
    if any(not _string(item.get(key)) for key in ("terminal_id", "workspace_id", "tab_id")):
        raise BackendError("missing pane identity")
    if not _integer(item.get("revision")) or not _string(item.get("agent_status")) or item["agent_status"] not in _STATES:
        raise BackendError("invalid pane revision or status")
    kind = item.get("agent")
    if (agent or kind is not None) and not _string(kind):
        raise BackendError("invalid agent identity")
    native = item.get("agent_session")
    if native is not None:
        if (not isinstance(native, dict) or not _string(native.get("kind")) or native["kind"] not in {"id", "path"}
                or any(not _string(native.get(key)) for key in ("source", "agent", "value"))
                or native["agent"] != kind):
            raise BackendError("invalid native agent session")
    for key in ("state_change_seq", "completion_seq"):
        if key in item and not _integer(item[key]):
            raise BackendError("invalid agent sequence")
    return item


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def _json(text: str) -> dict:
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BackendError("invalid CLI JSON") from exc
    if not isinstance(value, dict) or "error" in value:
        raise BackendError("unsuccessful CLI JSON response")
    return value


def _result(text: str, kind: str) -> dict:
    value = _json(text)
    result = value.get("result")
    if not isinstance(result, dict) or result.get("type") != kind or "error" in result:
        raise BackendError("invalid CLI response envelope")
    return result


def _rows(text: str, key: str, kind: str, *, agent: bool = False) -> list[dict]:
    rows = _result(text, kind).get(key)
    if not isinstance(rows, list):
        raise BackendError("missing inventory list")
    seen = set()
    for item in rows:
        _metadata(item, agent=agent)
        if item["pane_id"] in seen:
            raise BackendError("duplicate pane identity")
        seen.add(item["pane_id"])
    return rows


def _socket_path(env: dict, session: str | None) -> str:
    # Verified against herdr 0.9.3 `status server --json`: XDG_CONFIG_HOME (else
    # ~/.config)/herdr/herdr.sock for default, .../herdr/sessions/<name>/herdr.sock
    # for named sessions; HERDR_CONFIG_PATH does not move the socket.
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    root = os.path.join(os.path.expanduser(base), "herdr")
    if session not in (None, "default"):
        root = os.path.join(root, "sessions", session)
    return os.path.realpath(os.path.join(root, "herdr.sock"))


def _snapshot(item: dict) -> tuple:
    return (item["pane_id"], item["terminal_id"], item["workspace_id"], item["tab_id"],
            item.get("agent"), item.get("agent_session"), item["revision"],
            item["agent_status"], item.get("state_change_seq"), item.get("completion_seq"))


def _same_pane(agent: dict, pane: dict) -> bool:
    # The pane list need not expose agent-list unread-completion sequences.
    keys = ("pane_id", "terminal_id", "workspace_id", "tab_id", "agent",
            "agent_session", "revision", "agent_status")
    return all(agent.get(key) == pane.get(key) for key in keys)


class HerdrBackend:
    def __init__(self, config):
        self.config = config
        if not _string(config.executable):
            raise BackendError("invalid herdr executable")
        if (isinstance(config.timeout, bool) or not isinstance(config.timeout, (int, float))
                or not math.isfinite(config.timeout) or config.timeout <= 0):
            raise BackendError("invalid CLI timeout")
        self._env = os.environ.copy()
        inherited = self._env.get("HERDR_SOCKET_PATH")
        if inherited is not None:
            if not _string(inherited):
                raise BackendError("invalid herdr socket path")
            inherited = os.path.realpath(os.path.expanduser(inherited))
        if config.session is not None:
            if not isinstance(config.session, str) or not _SESSION.fullmatch(config.session):
                raise BackendError("invalid herdr session")
            # An explicit session wins; drop the conflicting socket for children only.
            self._env.pop("HERDR_SOCKET_PATH", None)
            self._prefix = (config.executable, "--session", config.session)
            self._target = _socket_path(self._env, config.session)
        elif inherited:
            self._env["HERDR_SOCKET_PATH"] = inherited
            self._prefix = (config.executable,)
            self._target = inherited
        else:
            self._prefix = (config.executable, "--session", "default")
            self._target = _socket_path(self._env, "default")
        # Explicit self_pane is trusted as configured. Herdr caller context only counts
        # when the caller's server is the monitored target, so pane IDs cannot alias.
        self._caller_pane = config.self_pane
        self._caller_context = False
        if (self._caller_pane is None and os.environ.get("HERDR_ENV")
                and os.environ.get("HERDR_PANE_ID") and inherited == self._target):
            self._caller_pane = os.environ["HERDR_PANE_ID"]
            self._caller_context = True
        if self._caller_pane is not None:
            _pane_id(self._caller_pane)
        self._self_terminal: str | None = None
        self._semaphore = asyncio.Semaphore(4)
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False

    @property
    def target(self) -> str:
        return self._target

    async def _collect(self, process) -> tuple[bytes, bytes]:
        count = 0

        async def read(stream):
            nonlocal count
            chunks = []
            while chunk := await stream.read(65536):
                count += len(chunk)
                if count > _MAX_OUTPUT:
                    raise BackendError("CLI output exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks)

        tasks = [asyncio.create_task(read(process.stdout)),
                 asyncio.create_task(read(process.stderr)),
                 asyncio.create_task(process.wait())]
        try:
            stdout, stderr, _ = await asyncio.gather(*tasks)
            return stdout, stderr
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _terminate(process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 0.5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()

    async def _execute(self, *argv: str, guard: Callable[[], bool] | None = None) -> str:
        if self._closed:
            raise BackendError("backend is closed", not_sent=True)
        process = None
        try:
            async with self._semaphore:
                if self._closed:
                    raise BackendError("backend is closed", not_sent=True)
                if guard is not None and not guard():
                    raise InputCancelled()
                try:
                    process = await asyncio.create_subprocess_exec(
                        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=self._env, limit=65536)
                except OSError as exc:
                    raise BackendError("CLI executable could not be started", not_sent=True) from exc
                self._processes.add(process)
                if self._closed:
                    raise BackendError("backend closed during command")
                try:
                    stdout, _ = await asyncio.wait_for(self._collect(process), self.config.timeout)
                except asyncio.TimeoutError as exc:
                    raise BackendError("CLI command timed out") from exc
                if process.returncode != 0:
                    # CLI error text can echo terminal content or command payloads.
                    raise BackendError(f"CLI command failed (exit {process.returncode})")
                try:
                    return stdout.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BackendError("invalid CLI output encoding") from exc
        finally:
            if process is not None:
                await asyncio.shield(self._terminate(process))
                self._processes.discard(process)

    async def _run(self, *args: str, guard: Callable[[], bool] | None = None) -> str:
        return await self._execute(*self._prefix, *args, guard=guard)

    @staticmethod
    async def _both(first, second) -> tuple[str, str]:
        """Run two commands; a failure cancels and reaps the sibling."""
        tasks = [asyncio.ensure_future(first), asyncio.ensure_future(second)]
        try:
            return tuple(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def inventory(self) -> Inventory:
        agents, panes = await self._both(self._run("agent", "list"), self._run("pane", "list"))
        return Inventory(_rows(agents, "agents", "agent_list", agent=True),
                         _rows(panes, "panes", "pane_list"))

    async def resolve_self(self) -> dict | None:
        """Return caller pane metadata, tracked by terminal ID across pane moves."""
        if not self._caller_pane:
            return None
        try:
            if self._self_terminal is None and self._caller_context:
                # Bare --current falls back to focus outside herdr; require the caller's ID.
                raw = await self._run("pane", "current", "--current")
                own = _metadata(_result(raw, "pane_current").get("pane"))
                if own["pane_id"] != self._caller_pane:
                    return None
            panes = _rows(await self._run("pane", "list"), "panes", "pane_list")
            if self._self_terminal is None:
                matches = [p for p in panes if p["pane_id"] == self._caller_pane]
                if self._caller_context:
                    matches = [p for p in matches if p["terminal_id"] == own["terminal_id"]]
            else:
                matches = [p for p in panes if p["terminal_id"] == self._self_terminal]
            if len(matches) != 1:
                return None
            self._self_terminal = matches[0]["terminal_id"]
            return matches[0]
        except BackendError:
            return None

    async def inspect(self, pane_id: str) -> Evidence:
        pane_id = _pane_id(pane_id)
        before = await self.inventory()
        agent = next((a for a in before.agents if a["pane_id"] == pane_id), None)
        pane = next((p for p in before.panes if p["pane_id"] == pane_id), None)
        if agent is None or pane is None:
            raise BackendError("agent or pane disappeared before inspection")
        explanation, text = await self._both(
            self._run("agent", "explain", pane_id, "--json"),
            self._run("pane", "read", pane_id, "--source", "detection", "--format", "text", "--lines", "120"))
        explain = _json(explanation)
        if (not _string(explain.get("agent")) or not _string(explain.get("state"))
                or explain["state"] not in _STATES):
            raise BackendError("invalid detection explanation")
        matched = explain.get("matched_rule")
        if "matched_rule" not in explain or (matched is not None and
                (not isinstance(matched, dict) or not _string(matched.get("id"))
                 or not _string(matched.get("state")) or matched["state"] not in _STATES)):
            raise BackendError("invalid detection rule")
        rule = matched["id"] if matched is not None else ""
        after = await self.inventory()
        new_agent = next((a for a in after.agents if a["pane_id"] == pane_id), None)
        new_pane = next((p for p in after.panes if p["pane_id"] == pane_id), None)
        # pi reports "blocked" itself from its UI-prompt hook; herdr's pi manifest has
        # no blocked screen rule, so detection cannot agree. For pi only, a non-blocked
        # explanation is tolerated: the menu text must then carry all of the evidence.
        self_reported = (agent["agent"] in SELF_REPORTED_BLOCKED and agent["agent_status"] == "blocked"
                         and explain["state"] != "blocked")
        stable = (new_agent is not None and new_pane is not None
                  and _snapshot(agent) == _snapshot(new_agent)
                  and _snapshot(pane) == _snapshot(new_pane)
                  and _same_pane(agent, pane) and _same_pane(new_agent, new_pane)
                  and explain["agent"] == agent["agent"]
                  and (explain["state"] == agent["agent_status"] or self_reported)
                  and (matched is None or matched["state"] == explain["state"]))
        if self_reported:
            rule = "self_reported_blocked"
        # Display authoritative list metadata even if explain reported idle instead of done.
        return Evidence(new_agent or agent, new_pane or pane, rule, text, stable)

    async def send_enter(self, pane_id: str, guard: Callable[[], bool] | None = None) -> None:
        """Submit Enter. `guard` runs after queueing, immediately before spawning;
        InputCancelled (not_sent=True) means nothing was sent. Any other error is uncertain."""
        try:
            pane_id = _pane_id(pane_id)
        except BackendError as exc:
            raise BackendError(str(exc), not_sent=True) from exc
        await self._run("pane", "send-keys", pane_id, "enter", guard=guard)

    async def legacy_writers(self) -> list[str]:
        output = await self._execute("ps", "-eo", "pid=,args=")
        writers = []
        for line in output.splitlines():
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            if len(parts) != 2 or not parts[0].isdigit():
                raise BackendError("invalid process inventory")
            if int(parts[0]) == os.getpid():
                continue
            try:
                words = shlex.split(parts[1])
            except ValueError:
                words = parts[1].split()
            if any(Path(word).name == "pane-dispatcher.py" for word in words):
                writers.append(f"{parts[0]}: pane-dispatcher.py")
        return writers

    async def close(self) -> None:
        """Refuse new commands and terminate running ones; their callers get BackendError."""
        self._closed = True
        await asyncio.gather(*(self._terminate(p) for p in tuple(self._processes)))
