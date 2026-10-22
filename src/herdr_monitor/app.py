"""Read-only presentation and explicit, fail-closed controls for the monitor."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, Static

from .config import Config
from .monitor import Controller


# All externally supplied content is literal text, never Rich markup or ANSI.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def literal(value: Any, limit: int = 1000) -> str:
    return _CONTROLS.sub(" ", _ANSI.sub("", str(value or "")))[:limit]


def age(timestamp: float | None, now: float | None = None) -> str:
    if timestamp is None:
        return "—"
    seconds = max(0, int((now if now is not None else time.time()) - timestamp))
    if seconds < 3:
        return "now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


STATUS_MARKS = {"working": ("●", "*"), "blocked": ("▲", "!"), "done": ("✓", "+"), "idle": ("○", "-"), "unknown": ("?", "?")}
LOG_FILTERS = [("All events", "all"), ("Status", "status"), ("Approval", "approval"), ("Errors", "errors")]
DARK = {"working": "#3987e5", "blocked": "#fab219", "done": "#0ca30c", "idle": "#93938c", "unknown": "#93938c", "error": "#d03b3b", "on": "#ec835a"}
LIGHT = {"working": "#2a78d6", "blocked": "#9c6800", "done": "#008300", "idle": "#72716b", "unknown": "#72716b", "error": "#b92f2f", "on": "#ad4f2e"}


def monitor_theme(light: bool = False, monochrome: bool = False) -> Theme:
    background, surface, foreground, muted, line = (
        ("#f5f5f2", "#ffffff", "#20201e", "#52514e", "#d5d5ce") if light
        else ("#1a1a19", "#242423", "#ffffff", "#c3c2b7", "#383835")
    )
    primary = muted if monochrome else ("#2a78d6" if light else "#3987e5")
    return Theme(
        name=f"herdr-{'light' if light else 'dark'}{'-mono' if monochrome else ''}",
        primary=primary, secondary=muted, accent=primary, foreground=foreground,
        background=background, surface=surface, panel=surface, dark=not light,
        warning=muted if monochrome else LIGHT["blocked"] if light else DARK["blocked"],
        error=muted if monochrome else LIGHT["error"] if light else DARK["error"],
        success=muted if monochrome else LIGHT["done"] if light else DARK["done"],
        variables={"monitor-muted": muted, "monitor-line": line,
                   "monitor-selection": ("#deded8" if light else "#383835") if monochrome else ("#e0e9f5" if light else "#283548"),
                   "monitor-off": "#deded8" if light else "#383835",
                   "monitor-dry": "#ead9ad" if light else "#55451e",
                   "monitor-on": "#ecd2c8" if light else "#583c30",
                   "monitor-badge-ink": foreground},
    )


@dataclass(frozen=True)
class ModeChoice:
    mode: str
    pane_id: str | None  # None means ALL agents


class ModeScreen(ModalScreen[ModeChoice | None]):
    """Pick a mode for ALL agents or only the selected one; applies immediately."""
    BINDINGS = [("escape", "cancel", "Cancel"), ("o", "pick('off')", "OFF"),
                ("d", "pick('dry-run')", "DRY-RUN"), ("n", "pick('on')", "ON"),
                ("tab", "toggle_scope", "Scope")]

    def __init__(self, pane_id: str | None = None, label: str = "") -> None:
        super().__init__()
        self.pane_id = pane_id
        self.label = label
        self.scope_all = True

    def compose(self) -> ComposeResult:
        with Vertical(id="mode-dialog", classes="dialog"):
            yield Label("Approval mode", classes="dialog-title")
            with Horizontal(id="mode-scope"):
                yield Button("ALL agents", id="scope-all")
                yield Button(Text(literal(self.label or "Selected agent unavailable", 40)), id="scope-one",
                             disabled=self.pane_id is None)
            yield Static("Tab: switch ALL / selected  ·  o / d / n: OFF / DRY-RUN / ON",
                         id="scope-hint", markup=False)
            yield Button("OFF  ·  observe only", id="mode-off")
            yield Button("DRY-RUN  ·  preview, never send", id="mode-dry-run")
            yield Button("ON  ·  confirm one-time Yes menus", id="mode-on")
            yield Button("Cancel", id="mode-cancel", classes="cancel")

    def on_mount(self) -> None:
        self._show_scope()

    def _show_scope(self) -> None:
        self.query_one("#scope-all", Button).set_class(self.scope_all, "scope-active")
        self.query_one("#scope-one", Button).set_class(not self.scope_all, "scope-active")

    def action_toggle_scope(self) -> None:
        if self.pane_id is not None:
            self.scope_all = not self.scope_all
            self._show_scope()

    def action_pick(self, mode: str) -> None:
        self.dismiss(ModeChoice(mode, None if self.scope_all else self.pane_id))

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        choice = event.button.id or ""
        if choice in {"scope-all", "scope-one"}:
            self.scope_all = choice == "scope-all"
            self._show_scope()
        elif choice == "mode-cancel":
            self.dismiss(None)
        else:
            self.action_pick(choice.removeprefix("mode-"))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class SettingsResult:
    config: Config
    mode: str
    log_filter: str
    save: bool


class SettingsScreen(ModalScreen[SettingsResult | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, config: Config, mode: str, log_filter: str) -> None:
        super().__init__()
        self.config = config
        self.mode = mode
        self.log_filter = log_filter

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog", classes="dialog"):
            yield Label("Monitor settings", classes="dialog-title")
            with VerticalScroll(id="settings-fields"):
                yield Label("Poll interval · seconds", classes="field-label")
                yield Input(str(self.config.interval), id="settings-interval", type="number")
                yield Label("Excluded pane IDs · comma-separated", classes="field-label")
                yield Input(", ".join(self.config.exclude), id="settings-exclude")
                yield Label("Appearance", classes="field-label")
                yield Select([("Dark", "dark"), ("Light", "light")], value=self.config.theme, allow_blank=False, id="settings-theme", compact=True)
                yield Label("Monitor events", classes="field-label")
                yield Select(LOG_FILTERS, value=self.log_filter, allow_blank=False, id="settings-log", compact=True)
                yield Label("Approval mode · ALL agents", classes="field-label")
                yield Select([("OFF", "off"), ("DRY-RUN", "dry-run"), ("ON", "on")], value=self.mode, allow_blank=False, id="settings-mode", compact=True)
                yield Static("Apply changes this run. Save also writes configuration; approval mode is never saved.", markup=False, classes="settings-note")
            yield Static("", id="settings-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="settings-cancel", classes="cancel")
                yield Button("Apply", id="settings-apply", variant="primary")
                yield Button("Save", id="settings-save")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "settings-cancel":
            self.dismiss(None)
            return
        try:
            updated = replace(
                self.config,
                interval=float(self.query_one("#settings-interval", Input).value),
                exclude=tuple(dict.fromkeys(part.strip() for part in self.query_one("#settings-exclude", Input).value.split(",") if part.strip())),
                theme=str(self.query_one("#settings-theme", Select).value),
            ).validated()
        except (ValueError, TypeError) as exc:
            self.query_one("#settings-error", Static).update(Text(literal(exc)))
            return
        self.dismiss(SettingsResult(updated, str(self.query_one("#settings-mode", Select).value), str(self.query_one("#settings-log", Select).value), event.button.id == "settings-save"))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close"), ("question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog", classes="dialog"):
            yield Label("Herdr monitor · shortcuts", classes="dialog-title")
            with VerticalScroll(id="help-copy"):
                yield Static(
                    "↑ / ↓     Select agent          /       Search workspace, session, pane or type\n"
                    "a         Mode (ALL / selected) s       Settings\n"
                    "e         Exclude / restore     m       Expand / restore log\n"
                    "p         Pause / follow log    Space   Emergency OFF\n"
                    "?         This help             q       Quit monitor\n\n"
                    "Space and q remain available in dialogs, except while typing in a text field. "
                    "Ctrl+C always quits. Pause changes log scrolling only; collection continues.\n\n"
                    "Last work is observed by this monitor, not historical activity. “done” "
                    "means unread completion, not process exit. Details show native identity; "
                    "actions always target pane IDs. Counts include filtered and excluded agents.\n\n"
                    "Mode dialog: Tab switches ALL / selected agent; o, d, n pick OFF, DRY-RUN, ON. "
                    "The MODE column shows each agent's effective mode. OFF never sends; DRY-RUN "
                    "only previews; ON confirms recognized one-time Yes menus (menu type only, not "
                    "command safety). ON is never persisted. Exit stops only this monitor.", markup=False)
            yield Button("Close", id="help-close")

    @on(Button.Pressed)
    def close_help(self) -> None:
        self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class MonitorScreen(Screen):
    def on_resize(self) -> None:
        if self.app._ui_mounted:
            self.app._resize_layout()
            self.app.refresh_view()


class MonitorApp(App[None]):
    CSS_PATH = str(Path(__file__).with_name("app.tcss"))
    TITLE = "Herdr Monitor"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("a", "mode", "Mode"), Binding("s", "settings", "Settings"),
        Binding("slash", "filter", "Filter", key_display="/"),
        Binding("e", "exclude", "Exclude"), Binding("m", "expand_log", "Log"),
        Binding("p", "follow", "Follow", show=False),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("space", "emergency_off", "OFF", key_display="Space", priority=True),
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("escape", "focus_table", "Agents", show=False),
    ]

    def __init__(self, controller: Controller, config: Config, requested_mode: str = "off", configure: bool = False) -> None:
        super().__init__()
        self.controller = controller
        self.config = config
        self.requested_mode = requested_mode
        self.configure = configure
        self.selected_id: str | None = None
        self.log_filter = "all"
        self.follow = True
        self.log_expanded = False
        self._poll_task: asyncio.Task[None] | None = None
        self._startup_handled = False
        self._columns: tuple[str, ...] = ()
        self._row_ids: list[str] = []
        self._cells: dict[str, tuple[Text, ...]] = {}
        self._log_seen = 0
        self._log_selection = "all"
        self._shutting_down = False
        self._ui_mounted = False
        for light in (False, True):
            for mono in (False, True):
                self.register_theme(monitor_theme(light, mono))
        self._apply_theme()

    def _apply_theme(self) -> None:
        self.theme = f"herdr-{self.config.theme}{'-mono' if self.config.no_color else ''}"

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("HERDR MONITOR", id="brand", markup=False)
            yield Static("Connecting…", id="connection", markup=False)
            yield Static("OFF", id="mode-badge", markup=False)
        yield Static("", id="summary", markup=False)
        with Horizontal(id="agent-controls"):
            yield Input(placeholder="Filter workspace / session / pane / type", id="agent-filter", compact=True)
            yield Select([("All states", "all")] + [(state.title(), state) for state in STATUS_MARKS], value="all", allow_blank=False, id="state-filter", compact=True)
            yield Static("", id="visible-count", markup=False)
        with Vertical(id="agents-panel", classes="panel"):
            yield DataTable(id="agents", cursor_type="row", show_row_labels=False, cell_padding=1, cursor_foreground_priority="renderable", cursor_background_priority="css")
            yield Static("No agents detected", id="empty-agents", markup=False)
        yield Static("Select an agent for details", id="details", classes="panel", markup=False)
        with Vertical(id="monitor-panel", classes="panel"):
            with Horizontal(id="log-controls"):
                yield Select(LOG_FILTERS, value="all", allow_blank=False, id="log-filter", compact=True)
                yield Button("Pause follow", id="follow-button", compact=True)
                yield Button("Expand", id="expand-button", compact=True)
            yield RichLog(id="monitor-log", max_lines=500, min_width=1, wrap=True, markup=False, highlight=False, auto_scroll=True)
        yield Static("Last work: observed here  ·  Space: emergency OFF", id="notice", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#agents-panel").border_title = "Agents"
        self.query_one("#details").border_title = "Details"
        self.query_one("#monitor-panel").border_title = "Monitor"
        self.controller.on_update = self.refresh_view
        # Controller starts OFF; requested modes are handled only after connection.
        self._ui_mounted = True
        self._resize_layout()
        self.refresh_view()
        self.query_one("#agents", DataTable).focus()
        self._poll_task = asyncio.create_task(self._run_controller())
        self.set_interval(1, self.refresh_view)
        if self.configure:
            self.action_settings()

    async def _run_controller(self) -> None:
        try:
            await self.controller.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.controller.set_mode("off")
            self._notice(f"Monitor stopped: {literal(exc)}")
        finally:
            if not self._shutting_down:
                self.refresh_view()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"emergency_off", "quit"} and isinstance(self.screen.focused, Input):
            # Input owns spaces and literal q; Ctrl+C remains an unconditional exit.
            return False
        return True

    async def _check_bindings(self, key: str, priority: bool = False) -> bool:
        if key == "ctrl+c" and priority:
            await self.action_quit()
            return True
        return await super()._check_bindings(key, priority)

    def get_default_screen(self) -> Screen:
        return MonitorScreen(id="_default")

    def _resize_layout(self) -> None:
        width, height = self.size
        screen = self.screen_stack[0]
        screen.set_class(width < 100, "compact")
        screen.set_class(width < 70, "narrow")
        screen.set_class(height < 29, "short")
        screen.set_class(height < 20, "tiny")
        screen.set_class(self.config.ascii, "ascii")
        screen.set_class(self.config.no_color, "mono")
        # WORKSPACE and SESSION identify the agent; narrower layouts drop TYPE, then LAST WORK, then WORKSPACE.
        columns = ("mark", "workspace", "session", "pane", "kind", "status", "work", "mode") if width >= 120 else (
            ("mark", "workspace", "session", "pane", "status", "work", "mode") if width >= 100 else
            ("mark", "workspace", "session", "pane", "status", "mode") if width >= 70 else
            ("mark", "session", "pane", "status", "mode")
        )
        if columns == self._columns:
            return
        table = self.query_one("#agents", DataTable)
        # Structural rebuilds occur only on size-breakpoint changes, never on polls.
        table.clear(columns=True)
        self._row_ids = []
        self._cells = {}
        self._columns = columns
        # Fixed widths + 2 padding per column + borders/margins (22 / 13 / 10 as in the original layout).
        reserved = 100 if width >= 120 else 91 if width >= 100 else 68 if width >= 70 else 50 if width >= 55 else 38
        widths = {"mark": 1, "workspace": 12 if width >= 100 else 9, "session": max(7, min(40, width - reserved)),
                  "pane": 12 if width >= 70 else 8, "kind": 7, "status": 9, "work": 9, "mode": 12}
        labels = {"mark": "", "workspace": "WORKSPACE", "session": "SESSION", "pane": "PANE ID", "kind": "TYPE",
                  "status": "STATUS", "work": "LAST WORK*", "mode": "MODE"}
        for column in columns:
            table.add_column(labels[column], key=column, width=widths[column])

    def _color(self, role: str) -> str | None:
        return None if self.config.no_color else (LIGHT if self.config.theme == "light" else DARK).get(role)

    def _marker(self, state: str) -> Text:
        marker = STATUS_MARKS.get(state, STATUS_MARKS["unknown"])[int(self.config.ascii)]
        return Text(marker, style=self._color(state) or "")

    def _plain(self, value: Any) -> Text:
        return Text(literal(value))

    def _mode_cell(self, record: Any) -> Text:
        """Effective per-agent mode; '*' marks a per-agent override of ALL."""
        blocked = "x" if self.config.ascii else "⊘"
        if record.pane_id == self.controller.self_id:
            return Text(f"{blocked} self", style=self._color("idle") or "")
        if record.excluded:
            return Text(f"{blocked} excluded", style=self._color("idle") or "")
        mode = self.controller.effective_mode(record.pane_id)
        role = {"on": "on", "dry-run": "blocked"}.get(mode, "idle")
        dot = {"on": ("◆", "#"), "dry-run": ("◇", "?")}.get(mode, ("○", "-"))[int(self.config.ascii)]
        text = Text(dot, style=self._color(role) or "")
        text.append(" " + mode.upper(), style=(self._color(role) or "") if mode != "off" else "")
        if record.pane_id in self.controller.overrides:
            text.append(" *", style=self._color("idle") or "")
        return text

    def _session_cell(self, record: Any) -> Text:
        """Agent conversation name (pi /rename); unnamed sessions show a dim dash."""
        if record.session_name:
            return self._plain(record.session_name)
        return Text("-" if self.config.ascii else "—", style=self._color("idle") or "")

    def _row(self, record: Any) -> tuple[Text, ...]:
        state = record.status if record.status in STATUS_MARKS else "unknown"
        status = self._marker(state)
        status.append(" " + state)
        work = "now" if state == "working" and self.controller.healthy else age(record.last_work)
        if self.config.ascii and work == "—":
            work = "-"
        cells = {"mark": Text((">" if self.config.ascii else "▸") if record.pane_id == self.selected_id else "", style=self._color("working") or ""),
                 "workspace": self._plain(record.workspace or record.workspace_id or "—"),
                 "session": self._session_cell(record),
                 "pane": self._plain(record.pane_id), "kind": self._plain(record.kind),
                 "status": status, "work": Text(work), "mode": self._mode_cell(record)}
        return tuple(cells[column] for column in self._columns)

    def refresh_view(self) -> None:
        if not self._ui_mounted or self._shutting_down:
            return
        records = list(self.controller.agents.values())
        connection = Text(literal(self.config.session or "current socket") + "  ")
        connected = self.controller.healthy
        connection.append(("*" if self.config.ascii else "●") if connected else "!", style=self._color("done" if connected else "blocked") or "")
        connection.append(" CONNECTED" if connected else " STALE")
        if not connected and self.controller.last_success is not None:
            connection.append(" · " + age(self.controller.last_success))
        self.query_one("#connection", Static).update(connection)
        badge = self.query_one("#mode-badge", Static)
        mode = self.controller.mode
        overrides = len(self.controller.overrides)
        badge.update(f" ALL {mode.upper()}" + (f" · {overrides} custom " if overrides else " "))
        badge.set_classes(f"badge-{'on' if 'on' in self.controller.overrides.values() else mode}")
        summary = Text(f"Agents {len(records)}")
        for state in ("working", "blocked", "done", "idle", "unknown"):
            count = sum(record.status == state for record in records)
            if self.screen_stack[0].has_class("narrow") and not count:
                continue
            summary.append("   ")
            summary.append_text(self._marker(state))
            summary.append(f" {state} {count}")
        self.query_one("#summary", Static).update(summary)
        query = self.query_one("#agent-filter", Input).value.casefold().strip()
        state_filter = self.query_one("#state-filter", Select).value
        visible = sorted((record for record in records if (state_filter == "all" or record.status == state_filter) and (not query or query in " ".join(literal(value) for value in (record.name, record.session_name, record.workspace, record.workspace_id, record.pane_id, record.kind)).casefold())), key=lambda record: ({"blocked": 0, "working": 1}.get(record.status, 2), record.pane_id))
        ids = [record.pane_id for record in visible]
        previous_index = self._row_ids.index(self.selected_id) if self.selected_id in self._row_ids else 0
        if self.selected_id not in ids:
            self.selected_id = ids[min(previous_index, len(ids) - 1)] if ids else None
        table = self.query_one("#agents", DataTable)
        for pane_id in self._row_ids:
            if pane_id not in ids:
                table.remove_row(pane_id)
                self._cells.pop(pane_id, None)
        for record in visible:
            cells = self._row(record)
            if record.pane_id not in self._cells:
                table.add_row(*cells, key=record.pane_id)
            elif cells != self._cells[record.pane_id]:
                for column, old, new in zip(self._columns, self._cells[record.pane_id], cells):
                    if old != new:
                        table.update_cell(record.pane_id, column, new)
            self._cells[record.pane_id] = cells
        if ids != self._row_ids:
            ranks = {pane_id: index for index, pane_id in enumerate(ids)}
            table.sort("pane", key=lambda value: ranks[value.plain])
            self._row_ids = ids
            if self.selected_id is not None:
                table.move_cursor(row=ids.index(self.selected_id), animate=False, scroll=False)
        self.query_one("#empty-agents").display = not bool(ids)
        self.query_one("#visible-count", Static).update(f"{len(ids)} / {len(records)}")
        self._refresh_details()
        self._refresh_log()
        if self.controller.journal.failed:
            self._notice("ERROR · " + literal(self.controller.journal.failed))
            self._status_notice = True
        elif not connected and self.controller.error:
            self._notice("STALE · " + literal(self.controller.error))
            self._status_notice = True
        elif getattr(self, "_status_notice", False):
            self._notice("")
            self._status_notice = False
        if not self._startup_handled and connected:
            self._startup_handled = True
            if self.requested_mode != "off":
                self.call_later(self._startup_mode)

    async def _startup_mode(self) -> None:
        # Do not place a second modal over startup settings; those take precedence.
        if not self.configure:
            await self._request_mode(self.requested_mode)

    def _refresh_details(self) -> None:
        widget = self.query_one("#details", Static)
        record = self.controller.agents.get(self.selected_id or "")
        if record is None:
            widget.border_title = "Details"
            widget.update("Select an agent for details")
            return
        widget.border_title = Text("Details · " + literal(record.pane_id))
        native = record.native_session
        identity = "unavailable" if not native else " · ".join(f"{literal(key)}={literal(value)}" for key, value in native.items())
        workspace = literal(record.workspace or "—") + (f" ({literal(record.workspace_id)})" if record.workspace_id else "")
        text = Text("Workspace: " + workspace + "    Name: " + literal(record.session_name or "—") + "\n")
        text.append("Session: " + identity + "\n")
        text.append("CWD: " + literal(record.cwd or "unavailable") + "    Mode: ")
        text.append_text(self._mode_cell(record))
        text.append(" (own setting)\n" if record.pane_id in self.controller.overrides else " (from ALL)\n")
        text.append("Reason: " + literal(record.reason or "—") + "\n")
        text.append(f"Work*: {age(record.last_work)} · Output while working*: {age(record.last_output)} · Transition*: {age(record.last_transition)} · Revision: {literal(record.revision)}")
        if self.config.ascii:
            text = Text(text.plain.replace("—", "-").replace("·", "/"))
        widget.update(text)

    @on(DataTable.RowHighlighted, "#agents")
    def select_row(self, event: DataTable.RowHighlighted) -> None:
        pane_id = event.row_key.value
        if pane_id not in self._row_ids or pane_id == self.selected_id:
            return
        old_id = self.selected_id
        self.selected_id = pane_id
        table = self.query_one("#agents", DataTable)
        for key in (old_id, pane_id):
            if key in self._cells and key in self.controller.agents:
                cells = self._row(self.controller.agents[key])
                table.update_cell(key, "mark", cells[0])
                self._cells[key] = cells
        self._refresh_details()

    @on(Input.Changed, "#agent-filter")
    @on(Select.Changed, "#state-filter")
    def filter_agents(self) -> None:
        self.refresh_view()

    @on(Select.Changed, "#log-filter")
    def filter_log(self, event: Select.Changed) -> None:
        self.log_filter = str(event.value)
        self._refresh_log()

    def _event_matches(self, event: Any) -> bool:
        kind = event.kind.upper()
        if self.log_filter == "status":
            return kind in {"STATE", "DISCOVERED", "LEFT", "IDENTITY", "CONNECTED", "STALE"}
        if self.log_filter == "approval":
            return kind in {"MODE", "WOULD_SEND", "SENT", "RESUMED", "UNCONFIRMED", "SKIPPED", "INTENT"}
        if self.log_filter == "errors":
            return any(part in kind for part in ("ERROR", "FAIL", "STALE", "UNCONFIRMED", "TIMEOUT"))
        return True

    def _refresh_log(self) -> None:
        log = self.query_one("#monitor-log", RichLog)
        events = list(self.controller.journal.events)
        if self.log_filter != self._log_selection:
            # Filtering is a user action; normal collection is append-only.
            log.clear()
            self._log_seen = 0
            self._log_selection = self.log_filter
        for event in events:
            if event.seq <= self._log_seen:
                continue
            self._log_seen = event.seq
            if not self._event_matches(event):
                continue
            kind = literal(event.kind).upper()
            glyph, ascii_glyph, role = {
                "DISCOVERED": ("+", "+", "idle"), "LEFT": ("−", "-", "idle"),
                "STATE": ("↻", "~", "working"), "MODE": ("⚙", "=", "blocked"),
                "WOULD_SEND": ("◇", "?", "blocked"), "SENT": ("→", ">", "on"),
                "RESUMED": ("✓", "+", "done"), "ERROR": ("!", "!", "error"),
                "UNCONFIRMED": ("!", "!", "error"),
            }.get(kind, ("·", ".", "error" if self.log_filter == "errors" else "idle"))
            line = Text(datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S") + "  ")
            line.append(ascii_glyph if self.config.ascii else glyph, style=self._color(role) or "")
            line.append(f" {kind:<11} {literal(event.pane_id, 24):<12} {literal(event.summary)}")
            log.write(line, scroll_end=self.follow)

    def _notice(self, message: str) -> None:
        self.query_one("#notice", Static).update(Text(literal(message)))

    async def _request_mode(self, mode: str, pane_id: str | None = None) -> None:
        # Health, self-identity and writer-lock checks are the controller's authority.
        ok, message = await self.controller.set_mode(mode, pane_id)
        scope = "ALL" if pane_id is None else literal(pane_id)
        self._notice(f"{scope} → {mode.upper()}" if ok else f"{mode.upper()} refused: {literal(message)}")
        self.refresh_view()

    async def _mode_chosen(self, choice: ModeChoice | None) -> None:
        if choice is not None:
            await self._request_mode(choice.mode, choice.pane_id)

    def action_mode(self) -> None:
        record = self.controller.agents.get(self.selected_id or "")
        if record is None or record.pane_id == self.controller.self_id or record.excluded:
            self.push_screen(ModeScreen(), self._mode_chosen)
        else:
            self.push_screen(ModeScreen(record.pane_id, f"{record.pane_id} · {record.session_name or record.name}"), self._mode_chosen)

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.config, self.controller.mode, self.log_filter), self._settings_chosen)

    async def _settings_chosen(self, result: SettingsResult | None) -> None:
        if result is None:
            return
        # OFF is applied before any potentially slow config/log-path work.
        if result.mode == "off":
            await self.controller.set_mode("off")
        ok, message = await self.controller.update_config(result.config)
        if not ok:
            self._notice(message or "Settings rejected")
            return
        self.config = result.config
        self._apply_theme()
        self._resize_layout()
        self.log_filter = result.log_filter
        self.query_one("#log-filter", Select).value = result.log_filter
        self._cells = {}  # Repaint style changes without deleting rows.
        table = self.query_one("#agents", DataTable)
        for pane_id in self._row_ids:
            if pane_id in self.controller.agents:
                self._cells[pane_id] = self._row(self.controller.agents[pane_id])
                for column, cell in zip(self._columns, self._cells[pane_id]):
                    table.update_cell(pane_id, column, cell)
        self.query_one("#monitor-log", RichLog).clear()
        self._log_seen = 0
        if result.save:
            try:
                self.config.save()
            except (OSError, ValueError) as exc:
                self._notice("Applied, but save failed: " + literal(exc))
                self.refresh_view()
                return
            self._notice("Settings saved · approval mode is not persisted")
        else:
            self._notice(message or "Settings applied for this run")
        self.refresh_view()
        if result.mode != self.controller.mode:
            await self._request_mode(result.mode)

    async def action_emergency_off(self) -> None:
        await self.controller.set_mode("off")
        self._notice("Emergency OFF · monitoring continues")
        self.refresh_view()

    def action_exclude(self) -> None:
        if self.selected_id is None:
            return
        if self.selected_id == self.controller.self_id:
            self._notice("This monitor is always excluded")
            return
        self.controller.toggle_exclude(self.selected_id)
        self.config = self.controller.config
        self.refresh_view()

    def action_filter(self) -> None:
        self.query_one("#agent-filter", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#agents", DataTable).focus()

    def action_follow(self) -> None:
        self.follow = not self.follow
        log = self.query_one("#monitor-log", RichLog)
        log.auto_scroll = self.follow
        if self.follow:
            log.scroll_end(animate=False)
        self.query_one("#follow-button", Button).label = "Pause follow" if self.follow else "Resume follow"

    def action_expand_log(self) -> None:
        self.log_expanded = not self.log_expanded
        self.screen_stack[0].set_class(self.log_expanded, "log-expanded")
        self.query_one("#expand-button", Button).label = "Restore" if self.log_expanded else "Expand"
        self.query_one("#monitor-log" if self.log_expanded else "#agents").focus()

    @on(Button.Pressed, "#follow-button")
    def follow_clicked(self) -> None:
        self.action_follow()

    @on(Button.Pressed, "#expand-button")
    def expand_clicked(self) -> None:
        self.action_expand_log()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def _stop_monitor(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.controller.on_update = None
        await self.controller.set_mode("off")
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self.controller.close()

    async def action_quit(self) -> None:
        await self._stop_monitor()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_monitor()
