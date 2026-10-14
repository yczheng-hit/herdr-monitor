"""Pilot checks use synthetic agents only; no herdr command is executed."""
import asyncio
from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import time

import pytest
from textual.widgets import Button, DataTable, Input, RichLog, Select, Static

from herdr_monitor.app import ModeScreen, MonitorApp, SettingsScreen, literal
from herdr_monitor.config import Config
from herdr_monitor.events import Event
from herdr_monitor.monitor import AgentRecord


class DemoJournal:
    def __init__(self):
        self.events = deque(maxlen=1000)
        self.seq = 0
        self.failed = ""

    def append(self, kind, pane_id="", summary=""):
        self.seq += 1
        self.events.append(Event(self.seq, time.time(), kind, pane_id, summary))


class DemoController:
    """Deterministic controller for tests and coordinator-generated screenshots."""
    def __init__(self, config: Config):
        self.config = config
        self.mode = "off"
        self.healthy = False
        self.last_success = None
        self.error = "connecting"
        self.self_id = "w1:p9"
        self.on_update = None
        self.journal = DemoJournal()
        self.overrides = {}
        self.mode_calls = []
        self.sent = []
        self.closed = False
        self.run_calls = 0
        self.poll_calls = 0
        now = time.time()
        self.agents = {
            "w1:p2": AgentRecord("w1:p2", "Pipeline review", "claude", "working", "t2", {"source": "reported", "agent": "claude", "kind": "id", "value": "session-demo-42"}, "/workspace/chip-design", 32, now, now - 8, now - 50, "working"),
            "w1:p3": AgentRecord("w1:p3", "SDK integration", "codex", "blocked", "t3", None, "/workspace/sdk", 18, now - 124, now - 140, now - 24, "one-time permission menu; OFF"),
            "w1:p4": AgentRecord("w1:p4", "Test bench", "claude", "done", "t4", None, "/workspace/tests", 8, now - 510, None, now - 10, "unread completion", True),
            "w1:p7": AgentRecord("w1:p7", "Shell helper", "codex", "idle", "t7", None, "/workspace/tools", 1, None, None, now - 300),
            "w1:p9": AgentRecord("w1:p9", "Monitor · self", "unknown", "idle", "t9", None, "/workspace/tools", 1, None, None, now - 300, "always excluded", True),
        }
        for record in self.agents.values():
            self.journal.append("DISCOVERED", record.pane_id, f"{record.name} · {record.kind}")
        self.journal.append("STATE", "w1:p3", "working → blocked")
        self.journal.append("MODE", summary="approval → OFF")

    async def poll_once(self):
        self.poll_calls += 1
        self.healthy = True
        self.last_success = time.time()
        self.error = ""
        if self.on_update:
            self.on_update()

    async def run(self):
        self.run_calls += 1
        await self.poll_once()
        await asyncio.Event().wait()

    def effective_mode(self, pane_id):
        record = self.agents.get(pane_id)
        if pane_id == self.self_id or (record and record.excluded):
            return "off"
        return self.overrides.get(pane_id, self.mode)

    async def set_mode(self, mode, pane_id=None):
        self.mode_calls.append((mode, pane_id) if pane_id else mode)
        if pane_id is None:
            self.mode = mode
            self.overrides.clear()
        else:
            self.overrides[pane_id] = mode
        self.journal.append("MODE", pane_id or "", f"→ {mode.upper()}")
        if self.on_update:
            self.on_update()
        return True, ""

    async def update_config(self, config):
        self.config = config
        for record in self.agents.values():
            record.excluded = record.pane_id in config.exclude or record.pane_id == self.self_id
        return True, ""

    def toggle_exclude(self, pane_id):
        record = self.agents[pane_id]
        record.excluded = not record.excluded
        self.config = replace(self.config, exclude=tuple(record.pane_id for record in self.agents.values() if record.excluded))

    async def close(self):
        self.closed = True


@pytest.fixture
def demo(tmp_path):
    config = Config(config_path=tmp_path / "config.json", log_path=tmp_path / "monitor.jsonl", session="default")
    controller = DemoController(config)
    return config, controller


def mode_column(app, pane_id):
    table = app.query_one("#agents", DataTable)
    return table.get_cell(pane_id, "mode").plain


@pytest.mark.asyncio
async def test_all_mode_applies_directly_without_confirmation(demo):
    config, controller = demo
    app = MonitorApp(controller, config)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert controller.run_calls == 1
        assert "MODE" in [str(c.label) for c in app.query_one("#agents", DataTable).columns.values()]
        assert "AUTO" not in [str(c.label) for c in app.query_one("#agents", DataTable).columns.values()]
        await pilot.press("a")
        assert isinstance(app.screen, ModeScreen)
        await pilot.click("#mode-on")
        await pilot.pause()
        assert not isinstance(app.screen, ModeScreen)  # no confirmation screen
        assert controller.mode_calls == ["on"]
        assert "ON" in mode_column(app, "w1:p2")
        assert "excluded" in mode_column(app, "w1:p4")
        assert "self" in mode_column(app, "w1:p9")
        assert "ALL ON" in str(app.query_one("#mode-badge", Static).content)
        await pilot.press("a")
        await pilot.press("space")  # emergency OFF works even over the dialog
        await pilot.pause()
        assert controller.mode == "off"
        await pilot.press("escape")
    assert controller.closed
    assert controller.mode == "off"


@pytest.mark.asyncio
async def test_selected_agent_override_and_keyboard_scope(demo):
    config, controller = demo
    app = MonitorApp(controller, config)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert app.selected_id == "w1:p3"
        await pilot.press("a", "tab", "n")  # selected scope, ON
        await pilot.pause()
        assert controller.mode_calls == [("on", "w1:p3")]
        assert controller.mode == "off"
        assert mode_column(app, "w1:p3").startswith("◆ ON")
        assert mode_column(app, "w1:p3").endswith("*")
        assert "OFF" in mode_column(app, "w1:p2")
        assert "1 custom" in str(app.query_one("#mode-badge", Static).content)
        await pilot.press("a", "d")  # ALL scope by default clears overrides
        await pilot.pause()
        assert controller.mode == "dry-run" and controller.overrides == {}
        assert "DRY-RUN" in mode_column(app, "w1:p3")
        await pilot.press("space")
        await pilot.pause()
        assert controller.mode == "off"


@pytest.mark.asyncio
async def test_typing_space_q_in_filter_does_not_trigger_actions(demo):
    config, controller = demo
    app = MonitorApp(controller, config)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("a", "n")
        await pilot.pause()
        assert controller.mode == "on"
        await pilot.press("slash", "q", "space")
        assert app.query_one("#agent-filter", Input).value == "q "
        assert controller.mode == "on" and not controller.closed


@pytest.mark.asyncio
async def test_filter_counts_selection_and_incremental_refresh(demo):
    config, controller = demo
    app = MonitorApp(controller, config)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = app.query_one("#agents", DataTable)
        assert app._row_ids == ["w1:p3", "w1:p2", "w1:p4", "w1:p7", "w1:p9"]
        await pilot.press("down")
        await pilot.pause()
        assert app.selected_id == "w1:p2"
        row = table.rows[next(key for key in table.rows if key.value == "w1:p2")]
        controller.agents["w1:p3"].status = "idle"
        controller.on_update()
        await pilot.pause()
        assert app.selected_id == "w1:p2"
        assert table.cursor_row == 0
        assert table.rows[next(key for key in table.rows if key.value == "w1:p2")] is row
        await pilot.press("slash")
        await pilot.press("c", "o", "d", "e", "x")
        await pilot.pause()
        assert set(app._row_ids) == {"w1:p3", "w1:p7"}
        assert "Agents 5" in str(app.query_one("#summary", Static).content)
        assert str(app.query_one("#visible-count", Static).content) == "2 / 5"
        app.query_one("#state-filter", Select).value = "blocked"
        await pilot.pause()
        assert app._row_ids == []
        assert app.query_one("#empty-agents").display


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (58, 20), (44, 16)])
async def test_responsive_render_ascii_light_and_resize(demo, size):
    config, controller = demo
    config = replace(config, theme="light", ascii=True, no_color=True)
    controller.config = config
    app = MonitorApp(controller, config)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        table = app.query_one("#agents", DataTable)
        selected = app.selected_id
        assert table.region.width <= size[0]
        assert app.query_one("#monitor-panel").region.bottom <= size[1]
        assert app.screen.has_class("ascii")
        assert app.screen.has_class("mono")
        assert "#2a78d6" not in app.export_screenshot()
        await pilot.press("m")
        await pilot.pause()
        assert not app.query_one("#agents-panel").display
        assert app.query_one("#monitor-panel").region.height >= size[1] - 6
        await pilot.press("m")
        await pilot.resize_terminal(120, 35)
        await pilot.pause()
        assert app.selected_id == selected
        assert app.query_one("#details").display


@pytest.mark.asyncio
async def test_settings_cancel_apply_validation_and_save_never_persist_on(demo):
    config, controller = demo
    app = MonitorApp(controller, config)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        assert isinstance(app.screen, SettingsScreen)
        app.screen.query_one("#settings-interval", Input).value = "5"
        await pilot.click("#settings-cancel")
        await pilot.pause()
        assert app.config.interval == 2
        assert not config.config_path.exists()
        await pilot.press("s")
        app.screen.query_one("#settings-interval", Input).value = "0"
        await pilot.click("#settings-apply")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.config.interval == 2
        app.screen.query_one("#settings-interval", Input).value = "3"
        app.screen.query_one("#settings-exclude", Input).value = "w1:p3, w1:p7"
        app.screen.query_one("#settings-theme", Select).value = "light"
        app.screen.query_one("#settings-mode", Select).value = "on"
        await pilot.click("#settings-save")
        await pilot.pause()
        assert controller.mode == "on"  # applied directly for this run
        saved = json.loads(config.config_path.read_text())
        assert "mode" not in saved and "approve" not in saved
        assert saved["interval"] == 3
        assert saved["exclude"] == ["w1:p3", "w1:p7"]
        assert saved["theme"] == "light"


@pytest.mark.asyncio
async def test_log_pause_filter_bounded_literal_and_exclude(demo):
    config, controller = demo
    app = MonitorApp(controller, config)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#monitor-log", RichLog)
        await pilot.press("p")
        assert not log.auto_scroll
        controller.journal.append("ERROR", "w1:p3", "[red]not markup[/red]\x1b[31m\x00")
        app.refresh_view()
        await pilot.pause()
        assert not log.auto_scroll
        assert any("[red]not markup[/red]" in line.text for line in log.lines)
        app.query_one("#log-filter", Select).value = "errors"
        await pilot.pause()
        assert any("ERROR" in line.text for line in log.lines)
        assert not any("DISCOVERED" in line.text for line in log.lines)
        await pilot.press("p")
        assert log.auto_scroll
        app.query_one("#agents", DataTable).focus()
        assert app.selected_id == "w1:p3"
        await pilot.press("e")
        assert controller.agents["w1:p3"].excluded
        app.selected_id = controller.self_id
        app.action_exclude()
        assert controller.agents[controller.self_id].excluded
        assert log.max_lines == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["off", "dry-run", "on"])
async def test_requested_modes_only_after_connected(demo, requested):
    config, controller = demo
    app = MonitorApp(controller, config, requested_mode=requested)
    assert controller.mode == "off"
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert controller.poll_calls == 1
        assert controller.mode == requested


@pytest.mark.asyncio
async def test_configure_startup_and_quit_ctrl_c_from_input(demo):
    config, controller = demo
    app = MonitorApp(controller, config, configure=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        app.screen.query_one("#settings-exclude", Input).focus()
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert controller.closed


def test_literal_external_content():
    assert literal("[bold]name[/bold]\x1b[31m\x00") == "[bold]name[/bold] "
    assert "\x1b" not in literal("\x1b]title\x07hello")
