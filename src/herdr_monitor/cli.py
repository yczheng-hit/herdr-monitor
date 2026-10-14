"""Foreground entry point; checks never send keys."""

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys

from .backend import BackendError, HerdrBackend
from .config import Config
from .monitor import Controller


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Foreground herdr agent monitor. Approval defaults OFF.")
    result.add_argument("--approve", choices=("off", "dry-run", "on"), default=None,
                        help="set ALL agents' startup mode; ON applies directly after connection checks")
    result.add_argument("--interval", type=float)
    result.add_argument("--timeout", type=float)
    result.add_argument("--cooldown", type=float)
    result.add_argument("--exclude", nargs="*", metavar="PANE_ID")
    result.add_argument("--extra-rules", nargs="*", metavar="RULE")
    result.add_argument("--log", type=Path)
    result.add_argument("--config", type=Path, default=Config().config_path)
    result.add_argument("--session", help="explicit existing session; overrides inherited socket context")
    result.add_argument("--herdr", dest="executable")
    result.add_argument("--self-pane", help="explicit own pane ID, verified against target inventory")
    result.add_argument("--theme", choices=("dark", "light"))
    result.add_argument("--ascii", action="store_true", default=None)
    result.add_argument("--no-color", action="store_true", default=None)
    result.add_argument("--configure", action="store_true", help="open settings on startup")
    result.add_argument("--check", action="store_true", help="print read-only connection check; no TUI or input")
    return result


def load_config(args, env=None) -> tuple[Config, str]:
    env = os.environ if env is None else env
    config = Config.load(args.config.expanduser())
    updates = {}
    for source, field in (("INTERVAL", "interval"), ("LOG", "log_path")):
        if source in env:
            updates[field] = float(env[source]) if field == "interval" else Path(env[source]).expanduser()
    if "EXCLUDE" in env:
        updates["exclude"] = tuple(env["EXCLUDE"].split())
    if "CLAUDE_EXTRA_APPROVE_RULES" in env:
        updates["extra_rules"] = tuple(filter(None, re.split(r"[\s,]+", env["CLAUDE_EXTRA_APPROVE_RULES"])))
    for field in ("interval", "timeout", "cooldown", "session", "executable", "self_pane", "theme", "ascii", "no_color"):
        value = getattr(args, field)
        if value is not None:
            updates[field] = value
    if args.exclude is not None:
        updates["exclude"] = tuple(args.exclude)
    if args.extra_rules is not None:
        updates["extra_rules"] = tuple(args.extra_rules)
    if args.log is not None:
        updates["log_path"] = args.log.expanduser()
    if env.get("NO_COLOR") is not None and args.no_color is None:
        updates["no_color"] = True
    mode = args.approve or ("dry-run" if env.get("DRY_RUN") == "1" else "off")
    return replace(config, **updates).validated(), mode


async def check(config: Config) -> int:
    backend = HerdrBackend(config)
    try:
        inventory = await backend.inventory()
        own = await backend.resolve_self()
        print(json.dumps({"target": backend.target, "agent_count": len(inventory.agents),
                          "pane_count": len(inventory.panes), "self_pane": own.get("pane_id") if own else None,
                          "approval": "OFF (check never submits input)",
                          "log_path": str(config.log_path), "interval": config.interval,
                          "python": sys.version.split()[0]}, ensure_ascii=False, indent=2))
        if not own:
            print("Note: self identity unavailable outside a herdr shell; ON will stay disabled.", file=sys.stderr)
        return 0
    except (BackendError, OSError) as exc:
        print(f"Connection check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await backend.close()


def main(argv=None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        config, requested_mode = load_config(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        arguments.error(str(exc))
    if args.check:
        return asyncio.run(check(config))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Open a terminal inside herdr to run the TUI, or use --check.", file=sys.stderr)
        return 2
    from .app import MonitorApp
    controller = Controller(HerdrBackend(config), config)
    try:
        MonitorApp(controller, config, requested_mode=requested_mode, configure=args.configure).run()
    except KeyboardInterrupt:
        pass
    return 0
