import json
from pathlib import Path

import pytest

from herdr_monitor.cli import load_config, parser
from herdr_monitor.config import Config
from herdr_monitor.events import EventJournal, WriterLock, clean


def test_configuration_precedence(tmp_path):
    path = tmp_path / "config.json"
    Config(interval=7, config_path=path, log_path=tmp_path / "log").save()
    args = parser().parse_args(["--config", str(path), "--interval", "3"])
    cfg, mode = load_config(args, {"INTERVAL": "5", "DRY_RUN": "0"})
    assert cfg.interval == 3 and mode == "off"
    _, mode = load_config(args, {"DRY_RUN": "1"})
    assert mode == "dry-run"


@pytest.mark.parametrize("value", [-1, 0, float("inf"), float("nan"), True])
def test_invalid_interval(value):
    with pytest.raises(ValueError):
        Config(interval=value).validated()


def test_corrupt_config_and_forbidden_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"mode":"on"}')
    with pytest.raises(ValueError):
        Config.load(path)
    path.write_text("[]")
    with pytest.raises(ValueError):
        Config.load(path)


def test_bounded_rotating_private_audit(tmp_path):
    path = tmp_path / "monitor.jsonl"
    journal = EventJournal(path, "test", max_events=3, max_bytes=250, backups=2)
    for i in range(20):
        assert journal.append("STATE", "w1:p1", f"test {i}")
    journal.close()
    assert len(journal.events) == 3
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(str(path) + ".1").exists()
    assert len(list(tmp_path.glob("monitor.jsonl*"))) <= 3
    for line in path.read_text().splitlines():
        assert json.loads(line)["target"] == "test"


def test_lock_target_scope(tmp_path):
    first = WriterLock("server", tmp_path)
    second = WriterLock("server", tmp_path)
    other = WriterLock("another", tmp_path)
    assert first.acquire()
    assert not second.acquire()
    assert other.acquire()
    first.release()
    assert second.acquire()
    second.release()
    other.release()


def test_plain_text_cleaner():
    assert clean("\x1b[31mred\x1b[0m\n[bold]") == "red[bold]"
