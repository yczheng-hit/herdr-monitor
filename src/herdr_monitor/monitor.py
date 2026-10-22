"""Observation and guarded, auditable input submission independent of the UI."""

import asyncio
from dataclasses import dataclass, replace
from typing import Callable
import json
import time

from .approval import classify
from .backend import BackendError
from .config import Config
from .events import EventJournal, WriterLock, clean


STATES = ("working", "blocked", "idle", "done", "unknown")
PI_TITLE_PREFIXES = ("π - ", "pi - ")


def session_name(kind: str, title: str, cwd: str = "") -> str:
    """Display-only conversation name from the agent's terminal title.

    pi titles are `π - <session name> - <cwd basename>` or `π - <cwd basename>` when unnamed,
    and are refreshed by pi whenever the name changes (/name, /rename, resume). Other agents
    (Claude, Codex) put their own conversation title in the terminal title, which herdr strips
    of spinner glyphs. Never used to route input.
    """
    title = clean(title, 200).strip()
    if kind != "pi":
        return title
    prefix = next((p for p in PI_TITLE_PREFIXES if title.startswith(p)), None)
    if prefix is None:
        return title  # an extension replaced pi's title; show it as-is
    rest = title[len(prefix):]
    base = cwd.rstrip("/").rsplit("/", 1)[-1]
    if rest == base:
        return ""
    head, sep, _ = rest.rpartition(" - ")
    return head.strip() if sep else ""


@dataclass
class AgentRecord:
    pane_id: str
    name: str
    kind: str
    status: str
    terminal_id: str
    native_session: dict | None
    cwd: str
    revision: int
    last_work: float | None = None
    last_output: float | None = None
    last_transition: float = 0.0
    reason: str = "not inspected"
    excluded: bool = False
    workspace_id: str = ""
    workspace: str = ""  # herdr workspace label; empty when unavailable
    session_name: str = ""  # agent's own conversation name (pi: /rename, /name); display only

    @property
    def identity(self) -> tuple:
        return self.terminal_id, self.kind, json.dumps(self.native_session, sort_keys=True)


@dataclass
class Pending:
    fingerprint: str
    started: float
    identity: tuple


class Controller:
    def __init__(self, backend, config: Config, journal: EventJournal | None = None,
                 writer_lock: WriterLock | None = None, clock=time.time, monotonic=time.monotonic):
        self.backend = backend
        self.config = config
        self.journal = journal or EventJournal(config.log_path, backend.target)
        self.writer_lock = writer_lock or WriterLock(backend.target)
        self.clock = clock
        self.monotonic = monotonic
        self.agents: dict[str, AgentRecord] = {}
        self.mode = "off"  # ALL agents, unless overridden per pane
        self.overrides: dict[str, str] = {}
        self.healthy = False
        self.last_success: float | None = None
        self.error = "connecting"
        self.self_id: str | None = None
        self._self_terminal: str | None = None
        self._generation = 0
        self._closed = False
        self._poll_lock = asyncio.Lock()
        self._pending: dict[str, Pending] = {}
        self._seen: dict[str, set[str]] = {}
        self._cooldown: dict[str, float] = {}
        self._last_decision: dict[str, str] = {}
        self.on_update: Callable[[], None] | None = None
        self.workspace_labels: dict[str, str] = {}
        self._event("START", summary="foreground observation started; approval OFF")

    def _notify(self) -> None:
        if self.on_update:
            self.on_update()

    def _event(self, kind: str, pid: str = "", summary: str = "", *, durable: bool = False) -> bool:
        success = self.journal.append(kind, pid, summary, durable=durable)
        if not success:
            self.error = self.journal.failed or "audit unavailable"
            self._disarm("audit failure")
        return success

    def effective_mode(self, pid: str) -> str:
        record = self.agents.get(pid)
        if pid == self.self_id or pid in self.config.exclude or (record and record.excluded):
            return "off"
        return self.overrides.get(pid, self.mode)

    def _any_on(self) -> bool:
        return self.mode == "on" or "on" in self.overrides.values()

    def _disarm(self, reason: str) -> None:
        """Fail closed: every agent returns to OFF."""
        self._generation += 1
        active = self.mode != "off" or bool(self.overrides)
        self.mode = "off"
        self.overrides.clear()
        self.writer_lock.release()
        if active:
            self.journal.append("MODE", summary=f"ALL → OFF: {clean(reason)}")

    async def _arm_checks(self, generation: int) -> str:
        if not self.healthy or self.last_success is None or self.clock() - self.last_success > max(10, self.config.interval * 3):
            return "fresh connected inventory is required"
        if not self.self_id or not self._self_terminal:
            return "monitor's own pane could not be verified; start inside herdr"
        if self.journal.failed:
            return self.journal.failed
        if self.writer_lock.fd is not None:
            return ""
        try:
            writers = await self.backend.legacy_writers()
        except (BackendError, OSError) as exc:
            return clean(exc)
        if generation != self._generation or self._closed:
            return "mode change cancelled"
        if writers:
            return "legacy dispatcher detected; stop it manually before enabling ON"
        try:
            if not self.writer_lock.acquire():
                return "another monitor owns the approval lock"
        except OSError as exc:
            return clean(exc)
        return ""

    async def set_mode(self, mode: str, pane_id: str | None = None) -> tuple[bool, str]:
        """Set ALL agents (pane_id None, clearing overrides) or one agent's override."""
        if mode not in {"off", "dry-run", "on"}:
            return False, "unknown approval mode"
        if pane_id is not None and (pane_id == self.self_id or pane_id not in self.agents):
            return False, "unknown or self pane"
        self._generation += 1
        generation = self._generation
        if mode == "on":
            refusal = await self._arm_checks(generation)
            if refusal:
                if not self._any_on():
                    self.writer_lock.release()
                self._notify()
                return False, refusal
        if generation != self._generation or self._closed:
            return False, "mode change cancelled"
        if pane_id is None:
            self.mode = mode
            self.overrides.clear()
            summary = f"ALL → {mode.upper()}"
        else:
            if mode == self.mode:
                self.overrides.pop(pane_id, None)
            else:
                self.overrides[pane_id] = mode
            summary = f"agent → {mode.upper()}"
        if not self._any_on():
            self.writer_lock.release()
        self._last_decision.clear()
        if not self._event("MODE", pane_id or "", summary, durable=True):
            return False, self.error
        self._notify()
        return True, ""

    async def update_config(self, config: Config) -> tuple[bool, str]:
        try:
            config.validated()
        except ValueError as exc:
            return False, str(exc)
        if (config.session, config.executable, config.self_pane) != (self.config.session, self.config.executable, self.config.self_pane):
            return False, "restart to change connection or self-pane settings"
        self._generation += 1  # invalidate in-flight input; modes are kept
        if config.log_path != self.config.log_path:
            candidate = EventJournal(config.log_path, self.backend.target)
            if not candidate.append("START", summary="audit location changed", durable=True):
                candidate.close()
                return False, candidate.failed
            old = self.journal
            candidate.events.extend(old.events)
            candidate.seq = old.seq + 1
            old.close()
            self.journal = candidate
        self.config = config
        self.backend.config = config
        self._last_decision.clear()
        self._event("CONFIG", summary="settings applied")
        self._notify()
        return True, ""

    def toggle_exclude(self, pid: str) -> None:
        if pid == self.self_id:
            return
        excluded = set(self.config.exclude)
        if pid in excluded:
            excluded.remove(pid)
        else:
            excluded.add(pid)
        self.config = replace(self.config, exclude=tuple(sorted(excluded)))
        self.backend.config = self.config
        self._generation += 1
        if pid in self.agents:
            self.agents[pid].excluded = pid in excluded
        self._event("CONFIG", pid, "excluded" if pid in excluded else "exclusion removed")
        self._notify()

    def _forget(self, pid: str) -> None:
        self.overrides.pop(pid, None)
        self._pending.pop(pid, None)
        self._seen.pop(pid, None)
        self._cooldown.pop(pid, None)
        self._last_decision.pop(pid, None)

    async def _refresh_workspaces(self) -> None:
        """Display-only workspace labels; failure keeps the last labels and never affects health."""
        list_workspaces = getattr(self.backend, "workspaces", None)
        if list_workspaces is None:
            return
        try:
            self.workspace_labels = await list_workspaces()
        except (BackendError, OSError, ValueError, KeyError, TypeError):
            pass

    async def poll_once(self) -> None:
        if self._closed or self._poll_lock.locked():
            return
        async with self._poll_lock:
            try:
                inventory = await self.backend.inventory()
                await self._refresh_workspaces()
                own = await self.backend.resolve_self()
                panes = {p["pane_id"]: p for p in inventory.panes}
                if own and own.get("terminal_id"):
                    self._self_terminal = own["terminal_id"]
                self.self_id = next((pid for pid, p in panes.items()
                                     if p.get("terminal_id") == self._self_terminal), None)
                if not self.self_id and self._any_on():
                    self._disarm("self identity lost")
                now = self.clock()
                fresh: dict[str, AgentRecord] = {}
                for item in inventory.agents:
                    pid = item["pane_id"]
                    pane = panes.get(pid)
                    if pid == self.self_id:
                        continue
                    merged = {**(pane or {}), **item}
                    kind = merged.get("agent", "unknown")
                    native = merged.get("agent_session")
                    cwd = clean(merged.get("foreground_cwd") or merged.get("cwd") or "", 1000)
                    title = merged.get("terminal_title_stripped") or merged.get("terminal_title") or merged.get("title") or ""
                    record = AgentRecord(pid, clean(merged.get("name") or merged.get("label")
                        or merged.get("terminal_title_stripped") or merged.get("title") or f"{kind} {pid}"),
                        kind, merged.get("agent_status", "unknown"), merged["terminal_id"],
                        native, cwd,
                        merged["revision"], last_transition=now, excluded=pid in self.config.exclude,
                        workspace_id=clean(merged.get("workspace_id") or "", 100),
                        workspace=clean(self.workspace_labels.get(merged.get("workspace_id"), ""), 200),
                        session_name=session_name(kind, title, cwd))
                    old = self.agents.get(pid)
                    if old and old.identity != record.identity:
                        self._event("LEFT", pid, "agent identity replaced")
                        self._forget(pid)
                        old = None
                    if old:
                        record.last_work = old.last_work
                        record.last_output = old.last_output
                        record.last_transition = old.last_transition
                        record.reason = old.reason
                        if old.status != record.status:
                            record.last_transition = now
                            self._event("STATE", pid, f"{old.status} → {record.status}")
                    else:
                        self._event("DISCOVERED", pid, f"{record.name} · {record.kind} · {record.status}")
                    if record.status == "working":
                        record.last_work = now
                        if old is None or old.revision != record.revision:
                            record.last_output = now
                    if not pane or pane.get("terminal_id") != record.terminal_id or pane.get("agent") != record.kind:
                        record.reason = "inconsistent inventory; no input"
                        record.excluded = True
                    pending = self._pending.get(pid)
                    if record.status != "blocked":
                        if pending and pending.identity == record.identity:
                            self._event("RESUMED", pid, f"observed {record.status} after confirmation; causality unverified")
                        self._pending.pop(pid, None)
                        self._seen.pop(pid, None)
                        self._last_decision.pop(pid, None)
                    elif pending and self.monotonic() - pending.started > max(15, self.config.interval * 5):
                        self._event("UNCONFIRMED", pid, "confirmation outcome uncertain; no automatic retry")
                        self._pending.pop(pid, None)
                        self._disarm("confirmation outcome uncertain")
                    fresh[pid] = record
                for pid in self.agents.keys() - fresh.keys():
                    self._event("LEFT", pid, "no longer in successful agent inventory")
                    if pid in self._pending:
                        self._disarm("pending target disappeared")
                    self._forget(pid)
                self.agents = fresh
                if not self._any_on():
                    self.writer_lock.release()
                recovered = not self.healthy
                self.healthy = True
                self.last_success = now
                self.error = self.journal.failed
                if recovered:
                    self._event("CONNECTED", summary="inventory refreshed; no automatic rearming")
                await self._inspect_blocked()
            except (BackendError, OSError, ValueError, KeyError, TypeError) as exc:
                message = clean(exc)
                if self.healthy or self.error != message:
                    self._event("ERROR", summary=f"backend unavailable: {message}")
                self.healthy = False
                self.error = message
                self._disarm("backend unavailable")
            finally:
                self._notify()

    async def _inspect_blocked(self) -> None:
        for record in list(self.agents.values()):
            if self._closed:
                return
            if record.status != "blocked" or record.excluded:
                continue
            evidence = await self.backend.inspect(record.pane_id)
            decision = classify(record.kind, evidence.rule, evidence.text, self.config.extra_rules)
            record.reason = decision.reason if evidence.stable else "changing or stale permission evidence"
            mode = self.effective_mode(record.pane_id)
            signature = f"{mode}:{record.reason}:{decision.fingerprint}"
            if self._last_decision.get(record.pane_id) != signature:
                self._last_decision[record.pane_id] = signature
                if mode == "dry-run" and evidence.stable and decision.eligible:
                    self._event("WOULD_SEND", record.pane_id, "one-time Yes selected; no input sent")
                elif mode != "off":
                    self._event("SKIP", record.pane_id, record.reason)
            if mode == "on" and evidence.stable and decision.eligible:
                await self._submit(record, decision.fingerprint)

    def _can_send(self, record: AgentRecord, generation: int) -> bool:
        return (not self._closed and self.effective_mode(record.pane_id) == "on" and self.healthy and
                not self.journal.failed and self.self_id is not None and
                record.pane_id != self.self_id and record.terminal_id != self._self_terminal and
                record.pane_id not in self.config.exclude and not record.excluded and
                generation == self._generation and self.writer_lock.fd is not None)

    async def _submit(self, record: AgentRecord, fingerprint: str) -> None:
        pid = record.pane_id
        if pid in self._pending or fingerprint in self._seen.get(pid, set()):
            return
        if self.monotonic() - self._cooldown.get(pid, float("-inf")) < self.config.cooldown:
            return
        generation = self._generation
        if not self._can_send(record, generation):
            return
        evidence = await self.backend.inspect(pid)
        decision = classify(record.kind, evidence.rule, evidence.text, self.config.extra_rules)
        identity = (evidence.agent.get("terminal_id"), evidence.agent.get("agent"),
                    json.dumps(evidence.agent.get("agent_session"), sort_keys=True))
        if not self._can_send(record, generation) or not evidence.stable or not decision.eligible or \
                decision.fingerprint != fingerprint or identity != record.identity or \
                evidence.agent.get("agent_status") != "blocked":
            self._event("SKIP", pid, "target or permission changed during revalidation")
            return
        if not self._event("INTENT", pid, "one-time confirmation; command safety not assessed", durable=True):
            return
        if not self._can_send(record, generation):
            return
        self._seen.setdefault(pid, set()).add(fingerprint)
        self._cooldown[pid] = self.monotonic()
        self._pending[pid] = Pending(fingerprint, self.monotonic(), record.identity)
        try:
            await self.backend.send_enter(pid, guard=lambda: self._can_send(record, generation))
        except asyncio.CancelledError:
            self._event("UNCONFIRMED", pid, "input submission interrupted; outcome uncertain")
            self._disarm("input interrupted")
            raise
        except (BackendError, OSError) as exc:
            if getattr(exc, "not_sent", False):
                self._pending.pop(pid, None)
                self._seen.get(pid, set()).discard(fingerprint)
                self._event("SKIP", pid, "input cancelled before submission")
                return
            self._event("ERROR", pid, f"input outcome uncertain: {clean(exc)}; no retry")
            self._disarm("input failure")
            return
        self._event("SENT", pid, "Enter submitted; waiting for observed state change", durable=True)

    async def run(self) -> None:
        while not self._closed:
            await self.poll_once()
            await asyncio.sleep(self.config.interval)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._disarm("monitor exiting")
        await self.backend.close()
        self._event("STOP", summary="monitor stopped; agents untouched")
        self.journal.close()
