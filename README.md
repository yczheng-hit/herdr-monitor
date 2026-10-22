# Herdr Monitor

A colorful foreground TUI for an **existing** herdr session. Open an ordinary shell inside herdr and run the monitor there. It creates no sessions, starts no agents, and does not continue after you quit.

## Run

From a shell inside herdr, foreground-run the monitor from the project directory:

```bash
./run-monitor.sh
./run-monitor.sh --configure
./run-monitor.sh --approve dry-run
```

Approval is **OFF by default**. `--approve on` sets ALL agents to ON immediately after connection/identity checks; there is no confirmation page. You can also change ALL or one selected agent in the mode dialog.

Connection checks do not send input or open a TUI:

```bash
./run-monitor.sh --check
```

Run inside the same herdr session you want to monitor. The inherited `HERDR_SOCKET_PATH` selects that server; without one the existing `default` session is used. `--session NAME` explicitly overrides inherited socket selection. No server is started or restarted. When the monitor cannot verify its own pane, observation still works but ON remains unavailable.

## Interface

- **Header:** connected/stale status, effective approval mode, refresh interval.
- **Summary:** all detected agents and counts by status; not a count of ordinary shell panes.
- **Agents:** **WORKSPACE** (herdr workspace label), **SESSION** (the agent's conversation name), pane ID, kind, status, last observed working time and effective **MODE** (OFF / DRY-RUN / ON). `*` marks a per-agent override; exclusions and the monitor itself cannot send input.
- **Details:** workspace label and ID, session name, full reported native conversation identity, directory, observed times and blocker reason.
- **Monitor:** timestamped state/approval/configuration/error events, also saved as private rotating JSONL.

Colors supplement symbols and labels: `● working`, `▲ blocked`, `✓ done`, `○ idle`, `? unknown`. Select `--theme light`, `--ascii`, or `--no-color` as needed. `NO_COLOR` is respected.

### Time and identity semantics

Herdr does not report historical work timestamps. `Last work` is the last time **this running monitor observed `working`**, not a timestamp recovered from before startup. A dash means work has not been observed. Output activity shown in details is a separate observation of revision changes while working.

- `done` means completion not yet seen, **not process exit**. Reading the monitor does not acknowledge completions.
- `blocked` may mean a question rather than a permission request.
- **SESSION** comes from the agent's terminal title. For pi it is the name set by `/rename` or `/name` (pi titles are `π - <name> - <dir>`); an unnamed pi session shows `—`. It updates on the next poll after a rename. Claude and Codex show their own conversation title. Narrower windows drop TYPE, then LAST WORK, then WORKSPACE.
- **WORKSPACE** comes from `herdr workspace list`. If that call fails, the last known labels (or the `w1` ID) are kept; it never affects connection health or approval.
- Native session IDs may be unavailable. The filter matches workspace, session, pane ID and type.
- Pane IDs route commands. Native conversation IDs and titles are never used as command targets.
- Connection failure retains the last good inventory with a stale indicator, rather than showing zero agents.

## Approval modes

Press **a** to open Mode. Select **ALL agents** or the selected agent, then click OFF / DRY-RUN / ON. Changes apply directly—no agreement or typing confirmation.

- Keyboard shortcuts inside Mode: **Tab** toggles ALL / selected; **o** = OFF, **d** = DRY-RUN, **n** = ON, **Esc** cancels.
- ALL applies to all current/future agents in the monitored herdr session and clears individual overrides.
- Per-agent settings apply only to that agent and show `*` in MODE. Other agents retain their modes.
- **Space** sets ALL to OFF and clears every override. Closing the monitor also stops everything it does. No daemon, nohup, autostart or background persistence is installed; keep it in your herdr pane for persistence.
- Mode settings are runtime-only and never saved. Backend/input/audit failures turn all agents OFF, including overrides.

| Mode | Behavior |
|---|---|
| OFF | Observation only; no pane input |
| DRY-RUN | Classify candidate permission menus and log `WOULD_SEND`; no input |
| ON | Guarded one-time Enter submission to eligible panes |

ON requires a verified self pane, fresh inventory, a working audit file and a target-scoped single-writer lock. Legacy `pane-dispatcher.py` processes must be stopped manually before arming; the monitor never kills them. Multiple read-only monitor instances are allowed.

### Important limitations

**This is UI permission recognition, not a command safety review.** An eligible command or edit can still be destructive. Only enable ON for agents/tasks you have authorized.

Herdr 0.9.3 has no semantic `approve` method and no atomic expected-menu/revision precondition. The monitor can only send keys. It requires a recognizable **current permission menu with a visibly selected one-time Yes**, rechecks the target and text, and then submits Enter. There remains a race between the last observation and key submission.

Supported agents: **Claude** (`❯`), **Codex** (`›`) and **pi** (`▶`, picc-permission-modes). For pi, only the Bash (`Do you want to proceed?`), edit-file and create-file panels are eligible; pi's untitled read and Allow/Block dialogs, plan review, select and input prompts are skipped. pi reports `blocked` itself from its UI-prompt hook, and herdr has no pi screen rule for it, so for pi a non-blocked detection result is tolerated and the on-screen menu is the only evidence.

It skips ambiguous layouts, ordinary questions, missing selection markers, selected No, persistent “always allow” options, incomplete menus and unsupported agents. It never navigates options, chooses a broader grant or answers arbitrary questions. Unknown permission wording degrades to observation, not guessed input.

A successful input write logs **SENT**, not APPROVED. A later nonblocked state logs **RESUMED**, which is observation—not proof of causality or safety. The same pending menu is not repeatedly confirmed. Uncertain outcomes, audit/input failures and backend loss disarm ON. Recovery does not automatically rearm it.

Use `Space` for immediate OFF. Quitting also disarms and stops observation without stopping other agents.

## Configuration

```bash
./run-monitor.sh --interval 3 --exclude w9:p4 w9:p7
./run-monitor.sh --theme light --ascii
./run-monitor.sh --log ~/tools/herdr-monitor-custom.jsonl
```

Use `--help` for all options. Settings are applied to the current run; save explicitly to `~/.config/herdr-monitor/config.json`. Approval ON is **never saved**.

Precedence: defaults → saved config → legacy environment → CLI → interactive changes.

Supported legacy environment variables: `INTERVAL`, `EXCLUDE`, `LOG`, `CLAUDE_EXTRA_APPROVE_RULES`, `DRY_RUN=1`. Unset/false `DRY_RUN` does **not** turn ON on. Extra rule names still require recognizable safe menu selection evidence.

Default audit: `~/.local/state/herdr-monitor/monitor.jsonl` (or under `$XDG_STATE_HOME`), maximum 5 MiB with three backups, file permissions `0600`. Default logs exclude raw terminal screens, command bodies and secret content. Pane names and routing identifiers can appear in the local audit; protect your log directory. Rotated logs are not unlimited history. Log writing failure disables input but leaves observation available.

## Installation

Python **3.11+** is required. If the default `python3` is older, use an explicit interpreter:

```bash
cd herdr-monitor
python3.11 -m venv .venv
.venv/bin/python -m pip install .
chmod +x run-monitor.sh
```

This changes only the project's virtual environment. It does not change system Python, herdr configuration, shell profiles or the original dispatcher scripts.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
```

Automated tests use synthetic inventories/menus and fake input submission. **Do not enable real ON or approve live prompts as a testing technique.** Live acceptance uses OFF and DRY-RUN. Headless Textual tests exercise the same UI/controller seam.
