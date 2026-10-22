"""WORKSPACE / SESSION columns: display-only identity from herdr workspaces and agent titles."""

import pytest
from textual.widgets import DataTable, Input, Static

from herdr_monitor.app import MonitorApp
from herdr_monitor.backend import BackendError, HerdrBackend

from herdr_monitor.monitor import session_name

from test_app import demo  # noqa: F401  (pytest fixture)
from test_backend import Adapter, config, envelope, clean_env  # noqa: F401  (autouse fixture)
from test_monitor import row as base_row, setup  # noqa: F401  (pytest fixture)


def row(**fields):
    item = base_row(**{k: fields.pop(k) for k in ("pid", "status") if k in fields})
    item.update(fields)
    return item


@pytest.mark.parametrize("kind,title,cwd,expected", [
    ("pi", "π - my-dev-setup", "/Users/me/tools/my-dev-setup", ""),          # unnamed
    ("pi", "π - 调度优化 - my-dev-setup", "/Users/me/tools/my-dev-setup", "调度优化"),
    ("pi", "π - a - b - repo", "/x/repo", "a - b"),                          # name containing " - "
    ("pi", "π - repo", "/x/repo/", ""),                                      # trailing slash in cwd
    ("pi", "pi - fix CI - repo", "/x/repo", "fix CI"),                        # PI_CONFIG_NAME builds
    ("pi", "custom title", "/x/repo", "custom title"),                        # extension-set title
    ("pi", "", "/x/repo", ""),
    ("claude", "查token", "/x", "查token"),
    ("codex", "  Review PR  ", "/x", "Review PR"),
    ("pi", "π - \x1b[31mred\x1b[0m - repo", "/x/repo", "red"),             # escapes stripped
])
def test_session_name_from_title(kind, title, cwd, expected):
    assert session_name(kind, title, cwd) == expected


async def test_controller_fills_workspace_and_session(setup):
    c, b = setup
    b.rows = [row(pid="w1:p1", status="idle", agent="pi", workspace_id="w2", cwd="/x/repo",
                  terminal_title_stripped="π - 调度优化 - repo")]
    labels = {"w2": "backend"}

    async def workspaces():
        return labels
    b.workspaces = workspaces
    await c.poll_once()
    record = c.agents["w1:p1"]
    assert (record.workspace_id, record.workspace, record.session_name) == ("w2", "backend", "调度优化")
    # A rename is picked up on the next poll without replacing the agent identity.
    b.rows[0]["terminal_title_stripped"] = "π - 新名字 - repo"
    await c.poll_once()
    assert c.agents["w1:p1"].session_name == "新名字"
    assert not [e for e in c.journal.events if e.kind == "LEFT"]


async def test_workspace_failure_keeps_monitoring_and_last_labels(setup):
    c, b = setup
    b.rows = [row(status="idle", workspace_id="w1")]
    calls = []

    async def workspaces():
        calls.append(1)
        if len(calls) > 1:
            raise BackendError("workspace list unavailable")
        return {"w1": "tools"}
    b.workspaces = workspaces
    await c.poll_once()
    await c.poll_once()
    assert c.healthy and c.agents["w1:p1"].workspace == "tools"
    assert not [e for e in c.journal.events if e.kind == "ERROR"]


async def test_backend_without_workspaces_shows_ids(setup):
    c, b = setup
    b.rows = [row(status="idle", workspace_id="w3")]
    await c.poll_once()
    assert (c.agents["w1:p1"].workspace, c.agents["w1:p1"].workspace_id) == ("", "w3")


async def test_backend_workspace_list(monkeypatch):
    rows = [{"workspace_id": "w1", "label": "tools", "number": 1},
            {"workspace_id": "w2", "label": "", "number": 2},       # no label: skipped
            {"workspace_id": "w3", "label": "a\x07b"}]               # control char: skipped
    adapter = Adapter(monkeypatch, {"workspace list": envelope("workspace_list", "workspaces", rows)})
    assert await HerdrBackend(config()).workspaces() == {"w1": "tools"}
    assert adapter.calls == [("herdr", "--session", "default", "workspace", "list")]


async def test_backend_workspace_list_rejects_bad_envelope(monkeypatch):
    Adapter(monkeypatch, {"workspace list": envelope("pane_list", "panes", [])})
    with pytest.raises(BackendError):
        await HerdrBackend(config()).workspaces()


def labels(app):
    return [str(c.label) for c in app.query_one("#agents", DataTable).columns.values()]


@pytest.mark.parametrize("size,expected", [
    ((140, 35), ["", "WORKSPACE", "SESSION", "PANE ID", "TYPE", "STATUS", "LAST WORK*", "MODE"]),
    ((110, 35), ["", "WORKSPACE", "SESSION", "PANE ID", "STATUS", "LAST WORK*", "MODE"]),
    ((80, 24), ["", "WORKSPACE", "SESSION", "PANE ID", "STATUS", "MODE"]),
    ((58, 20), ["", "SESSION", "PANE ID", "STATUS", "MODE"]),
])
async def test_columns_by_width(demo, size, expected):
    config_, controller = demo
    app = MonitorApp(controller, config_)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert labels(app) == expected
        table = app.query_one("#agents", DataTable)
        assert table.region.width <= size[0]
        assert sum(c.get_render_width(table) for c in table.columns.values()) <= table.region.width


async def test_cells_details_and_filter(demo):
    config_, controller = demo
    controller.agents["w1:p2"].workspace = "chip"
    controller.agents["w1:p2"].workspace_id = "w1"
    controller.agents["w1:p2"].session_name = "调度优化"
    app = MonitorApp(controller, config_)
    async with app.run_test(size=(140, 35)) as pilot:
        await pilot.pause()
        table = app.query_one("#agents", DataTable)
        assert table.get_cell("w1:p2", "workspace").plain == "chip"
        assert table.get_cell("w1:p2", "session").plain == "调度优化"
        assert table.get_cell("w1:p3", "session").plain == "—"   # unnamed
        assert table.get_cell("w1:p3", "workspace").plain == "—"  # demo record without workspace
        app.selected_id = "w1:p2"
        app._refresh_details()
        details = str(app.query_one("#details", Static).content)
        assert "Workspace: chip (w1)    Name: 调度优化\n" in details
        app.query_one("#agent-filter", Input).value = "调度"
        await pilot.pause()
        assert app._row_ids == ["w1:p2"]
        app.query_one("#agent-filter", Input).value = "chip"
        await pilot.pause()
        assert app._row_ids == ["w1:p2"]
