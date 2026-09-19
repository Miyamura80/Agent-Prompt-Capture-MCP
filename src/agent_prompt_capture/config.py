"""Paths, ``config.toml`` loading, token management and logging setup."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = [
    "Config",
    "apc_home",
    "ensure_home",
    "config_path",
    "db_path",
    "token_path",
    "log_path",
    "get_token",
    "rotate_token",
    "load_config",
    "setup_logging",
    "DEFAULT_HOME",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_IDLE_GAP_MINUTES",
    "DEFAULT_TAIL_MINUTES",
]

DEFAULT_HOME = Path("~/.agent-prompt-capture")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47821
DEFAULT_IDLE_GAP_MINUTES = 30.0
DEFAULT_TAIL_MINUTES = 5.0

LOG_NAME = "agent_prompt_capture"
_LOGGING_CONFIGURED = False


def apc_home() -> Path:
    """The runtime directory, honouring ``$APC_HOME``."""
    env = os.environ.get("APC_HOME")
    if env:
        return Path(env).expanduser()
    return DEFAULT_HOME.expanduser()


def ensure_home(home: Path | None = None) -> Path:
    """Create ``$APC_HOME`` with mode 0700 if needed and return it."""
    path = home or apc_home()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return path


def config_path(home: Path | None = None) -> Path:
    return (home or apc_home()) / "config.toml"


def db_path(home: Path | None = None) -> Path:
    return (home or apc_home()) / "prompts.db"


def token_path(home: Path | None = None) -> Path:
    return (home or apc_home()) / "token"


def log_path(home: Path | None = None) -> Path:
    return (home or apc_home()) / "apc.log"


def get_token(home: Path | None = None) -> str:
    """Read the shared secret, generating it on first use."""
    path = token_path(home)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    return rotate_token(home)


def rotate_token(home: Path | None = None) -> str:
    """Generate and persist a fresh shared secret (mode 0600)."""
    target = home or apc_home()
    ensure_home(target)
    path = token_path(target)
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    return token


def _hash_account(email: str) -> str:
    digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()
    return "sha256:" + digest[:12]


@dataclass
class Config:
    """The parsed ``config.toml``, plus the paths derived from ``$APC_HOME``."""

    home: Path = field(default_factory=apc_home)
    allowed_accounts: list[str] = field(default_factory=list)
    disabled_sources: list[str] = field(default_factory=list)
    account_aliases: dict[str, str] = field(default_factory=dict)
    extra_terms: list[str] = field(default_factory=list)
    extra_patterns: list[str] = field(default_factory=list)
    enable_ner: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    idle_gap_minutes: float = DEFAULT_IDLE_GAP_MINUTES
    tail_minutes: float = DEFAULT_TAIL_MINUTES

    # -- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return db_path(self.home)

    @property
    def token_path(self) -> Path:
        return token_path(self.home)

    @property
    def log_path(self) -> Path:
        return log_path(self.home)

    @property
    def config_path(self) -> Path:
        return config_path(self.home)

    # -- behaviour -----------------------------------------------------
    def get_token(self) -> str:
        return get_token(self.home)

    def rotate_token(self) -> str:
        return rotate_token(self.home)

    def is_account_allowed(self, email: str | None) -> bool:
        """Case-insensitive allowlist check. ``None``/empty is never allowed."""
        if not email:
            return False
        needle = email.strip().lower()
        if not needle:
            return False
        return any(needle == allowed.strip().lower() for allowed in self.allowed_accounts)

    def resolve_account(self, email: str | None) -> str | None:
        """Alias if configured, otherwise ``sha256:<12 hex>``. Never a raw email."""
        if not email:
            return None
        needle = email.strip()
        if not needle:
            return None
        lowered = needle.lower()
        for raw, alias in self.account_aliases.items():
            if raw.strip().lower() == lowered and alias:
                return alias
        return _hash_account(lowered)

    def is_source_disabled(self, source: object) -> bool:
        value = getattr(source, "value", source)
        return any(str(value) == str(d).strip() for d in self.disabled_sources)

    @classmethod
    def load(cls, home: Path | None = None) -> Config:
        return load_config(home)


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


def _as_float(value: object, default: float) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def load_config(home: Path | None = None) -> Config:
    """Load ``$APC_HOME/config.toml``. A missing or broken file yields defaults."""
    target = home or apc_home()
    cfg = Config(home=target)
    path = config_path(target)
    if not path.exists():
        return cfg

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        logging.getLogger(LOG_NAME).warning("could not parse %s, using defaults", path)
        return cfg

    capture = data.get("capture") or {}
    if isinstance(capture, dict):
        cfg.allowed_accounts = _as_str_list(capture.get("allowed_accounts"))
        cfg.disabled_sources = _as_str_list(capture.get("disabled_sources"))

    accounts = data.get("accounts") or {}
    if isinstance(accounts, dict):
        cfg.account_aliases = {str(k): str(v) for k, v in accounts.items()}

    pii = data.get("pii") or {}
    if isinstance(pii, dict):
        cfg.extra_terms = _as_str_list(pii.get("extra_terms"))
        cfg.extra_patterns = _as_str_list(pii.get("extra_patterns"))
        cfg.enable_ner = bool(pii.get("enable_ner", False))

    server = data.get("server") or {}
    if isinstance(server, dict):
        cfg.host = str(server.get("host", DEFAULT_HOST))
        try:
            cfg.port = int(server.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            cfg.port = DEFAULT_PORT

    time_cfg = data.get("time") or {}
    if isinstance(time_cfg, dict):
        cfg.idle_gap_minutes = _as_float(time_cfg.get("idle_gap_minutes"), DEFAULT_IDLE_GAP_MINUTES)
        cfg.tail_minutes = _as_float(time_cfg.get("tail_minutes"), DEFAULT_TAIL_MINUTES)

    return cfg


def setup_logging(home: Path | None = None, *, force: bool = False) -> logging.Logger:
    """Attach a rotating file handler to ``$APC_HOME/apc.log``.

    Never adds a stdout handler: hooks and the MCP server own stdout. A stderr
    handler is added only when ``APC_DEBUG=1``.
    """
    global _LOGGING_CONFIGURED
    logger = logging.getLogger(LOG_NAME)
    if _LOGGING_CONFIGURED and not force:
        return logger

    target = home or apc_home()
    logger.handlers.clear()
    logger.propagate = False
    debug = os.environ.get("APC_DEBUG") == "1"
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    try:
        ensure_home(target)
        handler: logging.Handler = RotatingFileHandler(
            log_path(target), maxBytes=1_048_576, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    except OSError:  # pragma: no cover - unwritable home
        logger.addHandler(logging.NullHandler())

    if debug:
        import sys

        stderr = logging.StreamHandler(sys.stderr)
        stderr.setFormatter(logging.Formatter("apc: %(levelname)s %(message)s"))
        logger.addHandler(stderr)

    _LOGGING_CONFIGURED = True
    return logger


def get_logger() -> logging.Logger:
    """The package logger (without forcing configuration)."""
    return logging.getLogger(LOG_NAME)
