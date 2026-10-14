"""Explicit configuration; approval ON is never persisted."""

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import tempfile


STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "herdr-monitor"


@dataclass(frozen=True)
class Config:
    interval: float = 2.0
    timeout: float = 5.0
    cooldown: float = 3.0
    session: str | None = None
    executable: str = "herdr"
    exclude: tuple[str, ...] = ()
    extra_rules: tuple[str, ...] = ()
    log_path: Path = STATE_DIR / "monitor.jsonl"
    config_path: Path = Path.home() / ".config" / "herdr-monitor" / "config.json"
    theme: str = "dark"
    ascii: bool = False
    no_color: bool = False
    self_pane: str | None = None

    def validated(self) -> "Config":
        for field, lower, upper in (("interval", 0.5, 60), ("timeout", 1, 60), ("cooldown", 3, 300)):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{field} must be between {lower} and {upper} seconds")
        if self.theme not in {"dark", "light"}:
            raise ValueError("theme must be dark or light")
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise ValueError("herdr executable is required")
        for field in ("session", "self_pane"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value)):
                raise ValueError(f"invalid {field}")
        for field in ("exclude", "extra_rules"):
            value = getattr(self, field)
            if not isinstance(value, tuple) or any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in value):
                raise ValueError(f"{field} must contain nonempty strings")
        for field in ("ascii", "no_color"):
            if not isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be a boolean")
        if not isinstance(self.log_path, Path) or not isinstance(self.config_path, Path):
            raise ValueError("log/config paths must be pathlib paths")
        if self.log_path.resolve() == self.config_path.resolve():
            raise ValueError("log and configuration must use different files")
        return self

    def save(self) -> None:
        self.validated()
        self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = asdict(self)
        data.pop("config_path")
        data["log_path"] = str(self.log_path)
        fd, path = tempfile.mkstemp(prefix=".config-", dir=self.config_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.config_path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls(config_path=path)
        if path.stat().st_size > 128 * 1024:
            raise ValueError("configuration is too large")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        allowed = set(cls.__dataclass_fields__) - {"config_path"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown configuration fields: {', '.join(sorted(unknown))}")
        for field in ("exclude", "extra_rules"):
            if field in data:
                if not isinstance(data[field], list):
                    raise ValueError(f"{field} must be a JSON array")
                data[field] = tuple(data[field])
        if "log_path" in data:
            if not isinstance(data["log_path"], str) or not data["log_path"]:
                raise ValueError("log_path must be a nonempty string")
            data["log_path"] = Path(data["log_path"]).expanduser()
        return cls(**data, config_path=path).validated()
