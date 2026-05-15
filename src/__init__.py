"""Telegram Claude Code Bridge.

A Telegram bridge for Claude Code with topic-scoped sessions and durable
delivery.
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# Read version from pyproject.toml when running from source (always current).
# Fall back to installed package metadata for pip installs without source tree.
_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
try:
    with open(_pyproject, "rb") as _f:
        __version__: str = tomllib.load(_f)["project"]["version"]
except Exception:
    try:
        __version__ = _pkg_version("telegram-claude-code-bridge")
    except PackageNotFoundError:
        __version__ = "0.0.0-dev"

__author__ = "Richard Atkinson, Fernando Taricano"
__email__ = ""
__license__ = "MIT"
__homepage__ = "https://github.com/ftaricano/telegram-claude-code-bridge"
