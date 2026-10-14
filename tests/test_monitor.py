from dataclasses import replace
from types import SimpleNamespace

import pytest

from herdr_monitor.config import Config
from herdr_monitor.events import EventJournal, WriterLock
from herdr_monitor.monitor import Controller


MENU = "Bash command\nprintf ok\nDo you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel"


def row(pid="w1:p1", status="blocked", revision=1, terminal="term1", **extra):
    return dict(pane_id=pid, terminal_id=terminal, workspace_id="w1", tab_id="w1:t1",
                agent="claude", agent_status=status, revision=revision, **extra)


class FakeBackend:
    target = "test-server"

    def __init__(self, config):
        self.config = config
        self.rows = [row()]
        self.own = dict(pane_id="w1:p0", terminal_id="self")
        self.sent = []
        self.text = MENU
        self.stable = True
        self.fail = None
        self.inspect_hook = None
        self.inspect_count = 0
        self.writers = []

    async def inventory(self):
        if self.fail:
            raise self.fail
        return SimpleNamespace(agents=self.rows, panes=[*self.rows, self.own])

    async def resolve_self(self):
        return self.own

    async def inspect(self, pid):
        self.inspect_count += 1
        if self.inspect_hook:
            await self.inspect_hook(self.inspect_count)
        item = next(r for r in self.rows if r["pane_id"] == pid)
        return SimpleNamespace(agent=item, pane=item, rule="bash_permission_prompt",
                               text=self.text, stable=self.stable)

    async def legacy_writers(self):
        return self.writers

    async def send_enter(self, pid, guard=None):
        if guard and not guard():
            from herdr_monitor.backend import BackendError
            error = BackendError("input cancelled before submission")
            error.not_sent = True
            raise error
        self.sent.append(pid)

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    config = Config(log_path=tmp_path / "audit.jsonl", config_path=tmp_path / "config.json")
    backend = FakeBackend(config)
    controller = Controller(backend, config,
                            writer_lock=WriterLock(backend.target, tmp_path / "locks"))
    yield controller, backend
    controller.writer_lock.release()
    controller.journal.close()


async def test_off_and_dry_never_send(setup):
    c, b = setup
    await c.poll_once()
    assert not b.sent
    assert c.agents["w1:p1"].last_work is None
    assert (await c.set_mode("dry-run"))[0]
    await c.poll_once()
    await c.poll_once()
    assert not b.sent
    assert sum(e.kind == "WOULD_SEND" for e in c.journal.events) == 1


async def test_on_sends_once_then_observes_resume(setup):
    c, b = setup
    await c.poll_once()
    assert (await c.set_mode("on"))[0]
    await c.poll_once()
    await c.poll_once()
    assert b.sent == ["w1:p1"]
    assert any(e.kind == "SENT" for e in c.journal.events)
    assert not any(e.kind == "APPROVED" for e in c.journal.events)
    b.rows = [row(status="working", revision=2)]
    await c.poll_once()
    assert c.agents["w1:p1"].last_work is not None
    assert any(e.kind == "RESUMED" for e in c.journal.events)


async def test_per_agent_override_only_sends_to_that_agent(setup):
    c, b = setup
    b.rows = [row(), row(pid="w1:p2", terminal="term2")]
    await c.poll_once()
    assert (await c.set_mode("on", "w1:p2"))[0]
    assert c.mode == "off" and c.effective_mode("w1:p2") == "on"
    assert c.effective_mode("w1:p1") == "off"
    await c.poll_once()
    assert b.sent == ["w1:p2"]
    # ALL clears per-agent overrides
    assert (await c.set_mode("dry-run"))[0]
    assert c.overrides == {} and c.effective_mode("w1:p2") == "dry-run"
    assert c.writer_lock.fd is None


async def test_override_of_self_or_unknown_refused(setup):
    c, b = setup
    await c.poll_once()
    assert not (await c.set_mode("on", "w1:p0"))[0]
    assert not (await c.set_mode("on", "missing"))[0]


async def test_failure_disarms_overrides_too(setup):
    from herdr_monitor.backend import BackendError
    c, b = setup
    await c.poll_once()
    await c.set_mode("on", "w1:p1")
    b.fail = BackendError("offline")
    await c.poll_once()
    assert c.overrides == {} and c.effective_mode("w1:p1") == "off"


async def test_exclusion_and_self(setup):
    c, b = setup
    b.rows.append(row(pid="w1:p0", terminal="self"))
    await c.poll_once()
    assert "w1:p0" not in c.agents
    c.toggle_exclude("w1:p1")
    await c.set_mode("on")
    await c.poll_once()
    assert not b.sent
    assert c.agents["w1:p1"].excluded


async def test_off_during_revalidation_cancels(setup):
    c, b = setup
    await c.poll_once()
    await c.set_mode("on")
    async def interrupt(count):
        if count == 3:
            await c.set_mode("off")
    b.inspect_hook = interrupt
    await c.poll_once()
    assert not b.sent
    assert c.mode == "off"


async def test_guard_blocks_queued_input(setup):
    c, b = setup
    await c.poll_once()
    await c.set_mode("on")
    original = b.send_enter

    async def switch_off(pid, guard=None):
        await c.set_mode("off")
        await original(pid, guard)

    b.send_enter = switch_off
    await c.poll_once()
    assert not b.sent
    assert c.mode == "off"
    assert c._pending == {}


async def test_failure_retains_inventory_disarms(setup):
    from herdr_monitor.backend import BackendError
    c, b = setup
    await c.poll_once()
    await c.set_mode("on")
    b.fail = BackendError("offline")
    await c.poll_once()
    assert len(c.agents) == 1
    assert not c.healthy
    assert c.mode == "off"
    b.fail = None
    await c.poll_once()
    assert c.healthy and c.mode == "off"


async def test_identity_reset_and_done_not_exit(setup):
    c, b = setup
    b.rows = [row(status="working")]
    await c.poll_once()
    b.rows = [row(status="done", revision=2)]
    await c.poll_once()
    assert c.agents["w1:p1"].last_work is not None
    assert c.agents["w1:p1"].status == "done"
    b.rows = [row(status="idle", terminal="new-term")]
    await c.poll_once()
    assert c.agents["w1:p1"].last_work is None


async def test_uncertain_outcome_never_retries(setup):
    c, b = setup
    now = [0.0]
    c.monotonic = lambda: now[0]
    await c.poll_once()
    await c.set_mode("on")
    await c.poll_once()
    now[0] = 100
    await c.poll_once()
    assert c.mode == "off"
    await c.set_mode("on")
    await c.poll_once()
    assert len(b.sent) == 1


async def test_legacy_writer_or_unknown_self_prevents_on(setup):
    c, b = setup
    await c.poll_once()
    b.writers = ["pane-dispatcher.py"]
    assert not (await c.set_mode("on"))[0]
    b.writers = []
    c.self_id = None
    assert not (await c.set_mode("on"))[0]


async def test_audit_failure_prevents_input(setup):
    c, b = setup
    await c.poll_once()
    c.journal.failed = "disk full"
    assert not (await c.set_mode("on"))[0]
    await c.poll_once()
    assert not b.sent


async def test_config_and_save_never_persist_on(setup):
    c, b = setup
    await c.poll_once()
    await c.set_mode("on")
    changed = replace(c.config, interval=3, exclude=("w1:p1",))
    assert (await c.update_config(changed))[0]
    assert c.mode == "on"  # settings keep the mode for this run
    assert c.effective_mode("w1:p1") == "off"  # newly excluded
    changed.save()
    assert "approve" not in changed.config_path.read_text()
    assert "mode" not in changed.config_path.read_text()


PI_MENU = ("─" * 40 + "\nBash command\n\n  make all\n\nThis command requires approval\n\n"
           "Do you want to proceed?\n▶ 1. Yes\n  2. Yes, and don't ask again for: make *\n  3. No\n\n"
           "Esc to cancel · Tab to amend · ctrl+e to explain\n")


async def test_pi_self_reported_block_dry_run_then_on(setup):
    c, b = setup
    b.rows = [{**row(), "agent": "pi"}]
    b.text = PI_MENU
    original = b.inspect

    async def pi_inspect(pid):
        evidence = await original(pid)
        evidence.rule = "self_reported_blocked"
        return evidence

    b.inspect = pi_inspect
    await c.poll_once()
    await c.set_mode("dry-run")
    await c.poll_once()
    assert not b.sent
    assert any(e.kind == "WOULD_SEND" for e in c.journal.events)
    await c.set_mode("on")
    await c.poll_once()
    await c.poll_once()
    assert b.sent == ["w1:p1"]


async def test_pi_plan_review_block_is_skipped(setup):
    c, b = setup
    b.rows = [{**row(), "agent": "pi"}]
    b.text = "─" * 40 + "\nPlan ready\n\n▶ 1. Approve\n  2. Keep planning\n"
    await c.poll_once()
    await c.set_mode("on")
    await c.poll_once()
    assert not b.sent
