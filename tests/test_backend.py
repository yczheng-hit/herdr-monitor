import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from herdr_monitor import backend as module
from herdr_monitor.backend import BackendError, HerdrBackend, InputCancelled


def pane(pid="w1:p1", status="blocked", revision=3, **extra):
    item = {"pane_id": pid, "terminal_id": f"term-{pid}", "workspace_id": "w1", "tab_id": "w1:t1",
            "revision": revision, "agent_status": status, "agent": "claude"}
    item.update(extra)
    return item


def envelope(kind, key, rows):
    return json.dumps({"id": f"cli:{kind}", "result": {key: rows, "type": kind}})


class FakeProcess:
    def __init__(self, stdout=b"", rc=0, hang=False):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.rc, self.hang, self.returncode = rc, hang, None
        self.terminated = False
        self._done = asyncio.Event()
        self.stdout.feed_data(stdout)
        if not hang:
            self._finish(rc)

    def _finish(self, rc):
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = rc
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self._finish(-15)

    kill = terminate


class Adapter:
    """Replaces process creation; responses are keyed by the herdr subcommand."""

    def __init__(self, monkeypatch, responses):
        self.calls, self.envs, self.processes = [], [], []
        self.responses = responses
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", self.spawn)

    async def spawn(self, *argv, env=None, **_):
        self.calls.append(argv)
        self.envs.append(env)
        key = " ".join(a for a in argv[1:] if not a.startswith("--") and a not in {"default", "named"})
        response = self.responses[next(k for k in self.responses if key.startswith(k))]
        if callable(response):
            response = response()
        if isinstance(response, FakeProcess):
            process = response
        elif isinstance(response, tuple):
            process = FakeProcess(response[0].encode(), response[1])
        else:
            process = FakeProcess(response.encode())
        self.processes.append(process)
        return process


def config(**overrides):
    values = {"executable": "herdr", "session": None, "timeout": 1.0, "self_pane": None}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("HERDR_SOCKET_PATH", "HERDR_ENV", "HERDR_PANE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def inventory_responses(agents=None, panes=None):
    agents = [pane()] if agents is None else agents
    panes = [pane()] if panes is None else panes
    return {"agent list": envelope("agent_list", "agents", agents),
            "pane list": envelope("pane_list", "panes", panes)}


async def test_inventory_uses_default_session_without_shell(monkeypatch, tmp_path):
    adapter = Adapter(monkeypatch, inventory_responses())
    backend = HerdrBackend(config())
    result = await backend.inventory()
    assert [a["pane_id"] for a in result.agents] == ["w1:p1"]
    assert sorted(adapter.calls) == [("herdr", "--session", "default", "agent", "list"),
                                     ("herdr", "--session", "default", "pane", "list")]
    assert backend.target == os.path.realpath(tmp_path / "config" / "herdr" / "herdr.sock")


async def test_empty_inventory_is_success(monkeypatch):
    Adapter(monkeypatch, inventory_responses([], []))
    result = await HerdrBackend(config()).inventory()
    assert result.agents == [] and result.panes == []


@pytest.mark.parametrize("agents", [
    "not json", '{"error": {"message": "x"}}', json.dumps({"result": {"type": "pane_list", "agents": []}}),
    envelope("agent_list", "agents", [pane(revision="3")]), envelope("agent_list", "agents", [pane(status="busy")]),
    envelope("agent_list", "agents", [{k: v for k, v in pane().items() if k != "terminal_id"}]),
    envelope("agent_list", "agents", [pane(agent=None)]), envelope("agent_list", "agents", [pane(), pane()]),
    envelope("agent_list", "agents", [pane(agent_session={"kind": "id", "source": "s", "agent": "codex", "value": "v"})]),
    '{"result": {"type": "agent_list", "agents": [], "agents": []}}', '{"result": {"type": "agent_list", "agents": [NaN]}}',
    (envelope("agent_list", "agents", []), 1),
])
async def test_inventory_rejects_failure_or_malformed_schema(monkeypatch, agents):
    Adapter(monkeypatch, {**inventory_responses(), "agent list": agents})
    with pytest.raises(BackendError):
        await HerdrBackend(config()).inventory()


async def test_failed_sibling_command_is_reaped(monkeypatch):
    hung = FakeProcess(hang=True)
    Adapter(monkeypatch, {"agent list": ("", 2), "pane list": hung})
    with pytest.raises(BackendError):
        await HerdrBackend(config()).inventory()
    assert hung.terminated and hung.returncode is not None


async def test_oversized_output_and_timeout_terminate(monkeypatch):
    big = FakeProcess(b"x" * (module._MAX_OUTPUT + 1), hang=True)
    Adapter(monkeypatch, {"agent list": big, "pane list": lambda: FakeProcess(hang=True)})
    backend = HerdrBackend(config(timeout=0.05))
    with pytest.raises(BackendError, match="size limit|timed out"):
        await backend.inventory()
    assert big.terminated


async def test_cancellation_reaps_process(monkeypatch):
    hung = FakeProcess(hang=True)
    Adapter(monkeypatch, {"pane send-keys": hung})
    task = asyncio.create_task(HerdrBackend(config(timeout=5)).send_enter("w1:p1"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hung.terminated


async def test_close_terminates_and_refuses_new_input(monkeypatch):
    hung = FakeProcess(hang=True)
    adapter = Adapter(monkeypatch, {"pane send-keys": hung})
    backend = HerdrBackend(config(timeout=5))
    task = asyncio.create_task(backend.send_enter("w1:p1"))
    await asyncio.sleep(0.01)
    await backend.close()
    with pytest.raises(BackendError) as running:
        await task
    assert hung.terminated and not running.value.not_sent
    with pytest.raises(BackendError) as later:
        await backend.send_enter("w1:p1")
    assert later.value.not_sent and len(adapter.calls) == 1


async def test_send_enter_exact_args_and_return_code(monkeypatch):
    adapter = Adapter(monkeypatch, {"pane send-keys": ""})
    backend = HerdrBackend(config(session="named"))
    await backend.send_enter("w1:p1")
    assert adapter.calls == [("herdr", "--session", "named", "pane", "send-keys", "w1:p1", "enter")]
    adapter.responses["pane send-keys"] = ("", 1)
    with pytest.raises(BackendError) as failure:
        await backend.send_enter("w1:p1")
    assert not failure.value.not_sent


@pytest.mark.parametrize("pid", ["w1:p1 enter", "--pane", "w1:p1;x", ""])
async def test_send_enter_rejects_unsafe_pane(monkeypatch, pid):
    adapter = Adapter(monkeypatch, {})
    with pytest.raises(BackendError) as failure:
        await HerdrBackend(config()).send_enter(pid)
    assert failure.value.not_sent and not adapter.calls


async def test_send_guard_runs_after_semaphore_before_spawn(monkeypatch):
    adapter = Adapter(monkeypatch, {"pane send-keys": ""})
    backend = HerdrBackend(config())
    allowed = True
    for _ in range(4):
        await backend._semaphore.acquire()
    task = asyncio.create_task(backend.send_enter("w1:p1", guard=lambda: allowed))
    await asyncio.sleep(0.01)
    allowed = False  # e.g. OFF while queued behind other commands
    for _ in range(4):
        backend._semaphore.release()
    with pytest.raises(InputCancelled) as cancelled:
        await task
    assert cancelled.value.not_sent and isinstance(cancelled.value, BackendError)
    assert not adapter.calls
    await backend.send_enter("w1:p1", guard=lambda: True)
    assert len(adapter.calls) == 1


async def test_timeout_after_spawn_is_uncertain(monkeypatch):
    Adapter(monkeypatch, {"pane send-keys": lambda: FakeProcess(hang=True)})
    with pytest.raises(BackendError) as failure:
        await HerdrBackend(config(timeout=0.05)).send_enter("w1:p1", guard=lambda: True)
    assert not failure.value.not_sent


async def test_spawn_failure_is_not_sent(monkeypatch):
    async def missing(*_, **__):
        raise FileNotFoundError()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", missing)
    with pytest.raises(BackendError) as failure:
        await HerdrBackend(config()).send_enter("w1:p1")
    assert failure.value.not_sent


async def test_semaphore_caps_concurrency(monkeypatch):
    active = peak = 0

    async def spawn(*_, **__):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        process = FakeProcess(hang=True)

        async def finish():
            await asyncio.sleep(0.01)
            nonlocal active
            active -= 1
            process._finish(0)
        asyncio.get_running_loop().create_task(finish())
        return process
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    backend = HerdrBackend(config())
    await asyncio.gather(*(backend.send_enter("w1:p1") for _ in range(10)))
    assert peak == 4


def test_session_and_inherited_socket_targets(monkeypatch, tmp_path):
    default = os.path.realpath(tmp_path / "config" / "herdr" / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "config" / "herdr" / "." / "herdr.sock"))
    inherited = HerdrBackend(config())
    assert inherited.target == default and inherited._prefix == ("herdr",)
    explicit = HerdrBackend(config(session="default"))
    assert explicit.target == default and "HERDR_SOCKET_PATH" not in explicit._env
    named = HerdrBackend(config(session="named"))
    assert named.target == os.path.realpath(tmp_path / "config" / "herdr" / "sessions" / "named" / "herdr.sock")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERDR_SOCKET_PATH")
    assert HerdrBackend(config()).target == os.path.realpath(tmp_path / "home" / ".config" / "herdr" / "herdr.sock")


@pytest.mark.parametrize("overrides", [{"session": "../x"}, {"session": "-x"}, {"self_pane": "focused"},
                                       {"timeout": 0}, {"timeout": float("nan")}, {"executable": ""}])
def test_invalid_configuration(overrides):
    with pytest.raises(BackendError):
        HerdrBackend(config(**overrides))


async def test_resolve_self_requires_explicit_or_caller_context(monkeypatch):
    adapter = Adapter(monkeypatch, inventory_responses())
    assert await HerdrBackend(config()).resolve_self() is None
    assert adapter.calls == []


async def test_resolve_explicit_self_uses_pane_list_and_tracks_moves(monkeypatch):
    panes = [pane("w1:p1"), pane("w1:p2", terminal_id="term-self")]
    adapter = Adapter(monkeypatch, inventory_responses(panes=panes))
    backend = HerdrBackend(config(self_pane="w1:p2"))
    assert (await backend.resolve_self())["terminal_id"] == "term-self"
    assert all("current" not in call for call in adapter.calls)
    adapter.responses["pane list"] = envelope("pane_list", "panes", [pane("w2:p9", terminal_id="term-self")])
    assert (await backend.resolve_self())["pane_id"] == "w2:p9"
    adapter.responses["pane list"] = envelope("pane_list", "panes", [pane("w1:p2")])
    assert await backend.resolve_self() is None


async def test_resolve_caller_context_rejects_focused_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "config" / "herdr" / "herdr.sock"))
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p2")
    focused = json.dumps({"result": {"type": "pane_current", "pane": pane("w1:p1")}})
    adapter = Adapter(monkeypatch, {**inventory_responses(), "pane current": focused})
    backend = HerdrBackend(config())
    assert await backend.resolve_self() is None
    adapter.responses["pane current"] = json.dumps({"result": {"type": "pane_current", "pane": pane("w1:p2")}})
    adapter.responses["pane list"] = envelope("pane_list", "panes", [pane("w1:p2")])
    assert (await backend.resolve_self())["pane_id"] == "w1:p2"
    assert ("herdr", "pane", "current", "--current") in adapter.calls


async def test_caller_context_on_other_server_is_ignored(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/other.sock")
    adapter = Adapter(monkeypatch, inventory_responses())
    assert await HerdrBackend(config(session="named")).resolve_self() is None
    assert adapter.calls == []


EXPLAIN = json.dumps({"agent": "claude", "state": "blocked",
                      "matched_rule": {"id": "bash_permission_prompt", "state": "blocked", "priority": 850}})


async def test_inspect_brackets_reads_with_inventory(monkeypatch):
    adapter = Adapter(monkeypatch, {**inventory_responses(), "agent explain": EXPLAIN, "pane read": "menu\n"})
    evidence = await HerdrBackend(config()).inspect("w1:p1")
    assert evidence.stable and evidence.rule == "bash_permission_prompt" and evidence.text == "menu\n"
    commands = [call[3:] for call in adapter.calls]
    assert ("agent", "explain", "w1:p1", "--json") in commands
    assert ("pane", "read", "w1:p1", "--source", "detection", "--format", "text", "--lines", "120") in commands
    assert commands.count(("agent", "list")) == 2


async def test_inspect_unstable_when_revision_changes(monkeypatch):
    revisions = iter([3, 4])

    def agents():
        return envelope("agent_list", "agents", [pane(revision=next(revisions))])
    Adapter(monkeypatch, {**inventory_responses(), "agent list": agents, "agent explain": EXPLAIN, "pane read": "x"})
    assert not (await HerdrBackend(config()).inspect("w1:p1")).stable


@pytest.mark.parametrize("explain", [
    json.dumps({"agent": "claude", "state": "idle", "matched_rule": None}),
    json.dumps({"agent": "codex", "state": "blocked", "matched_rule": None}),
])
async def test_inspect_unstable_on_explain_mismatch(monkeypatch, explain):
    Adapter(monkeypatch, {**inventory_responses(), "agent explain": explain, "pane read": "x"})
    assert not (await HerdrBackend(config()).inspect("w1:p1")).stable


@pytest.mark.parametrize("state", ["idle", "working", "unknown"])
async def test_inspect_pi_self_reported_block_is_stable(monkeypatch, state):
    rows = [pane(agent="pi")]
    explain = json.dumps({"agent": "pi", "state": state, "matched_rule": None})
    Adapter(monkeypatch, {**inventory_responses(rows, rows), "agent explain": explain, "pane read": "x"})
    evidence = await HerdrBackend(config()).inspect("w1:p1")
    assert evidence.stable and evidence.rule == "self_reported_blocked"


async def test_inspect_pi_still_requires_stable_identity(monkeypatch):
    revisions = iter([3, 4])

    def agents():
        return envelope("agent_list", "agents", [pane(agent="pi", revision=next(revisions))])
    explain = json.dumps({"agent": "pi", "state": "idle", "matched_rule": None})
    Adapter(monkeypatch, {**inventory_responses([pane(agent="pi")], [pane(agent="pi")]),
                          "agent list": agents, "agent explain": explain, "pane read": "x"})
    assert not (await HerdrBackend(config()).inspect("w1:p1")).stable


async def test_inspect_self_report_exception_is_pi_only(monkeypatch):
    explain = json.dumps({"agent": "claude", "state": "idle", "matched_rule": None})
    Adapter(monkeypatch, {**inventory_responses(), "agent explain": explain, "pane read": "x"})
    evidence = await HerdrBackend(config()).inspect("w1:p1")
    assert not evidence.stable and evidence.rule == ""


@pytest.mark.parametrize("explain", ["[]", '{"agent": "claude", "state": "blocked"}',
                                     '{"agent": "claude", "state": "blocked", "matched_rule": {"id": 1}}'])
async def test_inspect_rejects_malformed_explain(monkeypatch, explain):
    Adapter(monkeypatch, {**inventory_responses(), "agent explain": explain, "pane read": "x"})
    with pytest.raises(BackendError):
        await HerdrBackend(config()).inspect("w1:p1")


async def test_inspect_missing_agent(monkeypatch):
    Adapter(monkeypatch, inventory_responses(agents=[]))
    with pytest.raises(BackendError):
        await HerdrBackend(config()).inspect("w1:p1")


async def test_legacy_writers(monkeypatch):
    listing = (f"{os.getpid()} python3 /x/pane-dispatcher.py\n"
               " 42 /usr/bin/python3 /opt/tools/pane-dispatcher.py --once\n"
               " 43 vim pane-dispatcher.py.bak\n 44 python3 herdr-monitor\n")
    adapter = Adapter(monkeypatch, {"-eo": listing})
    assert await HerdrBackend(config()).legacy_writers() == ["42: pane-dispatcher.py"]
    assert adapter.calls == [("ps", "-eo", "pid=,args=")]


@pytest.mark.parametrize("response", [("", 1), "garbage line\n"])
async def test_legacy_writer_detection_fails_closed(monkeypatch, response):
    Adapter(monkeypatch, {"-eo": response})
    with pytest.raises(BackendError):
        await HerdrBackend(config()).legacy_writers()
