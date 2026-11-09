"""Builds anchor sinks from the environment.

GAWE_ANCHOR_SINKS   comma-separated sink names; unset or empty turns anchoring off
                    (available: file, rfc3161)
GAWE_ANCHOR_DIR     where sinks keep their records (default .anchors). Put it on different
                    storage from the database if it is meant to protect against its owner.
GAWE_ANCHOR_TSA     timestamp authority for rfc3161 (default digicert; or sectigo). Needs the
                    optional extra: pip install -e ".[anchor]".
"""

import os
from collections.abc import Mapping
from pathlib import Path

from gawe.anchoring.file_sink import FileAnchorSink
from gawe.core.anchor import AnchorSink

DEFAULT_DIR = ".anchors"
_AVAILABLE = "file, rfc3161"


class AnchorConfigError(ValueError):
    """The anchor settings name a sink or authority that does not exist, or repeat a sink."""


def sinks_from_env(env: Mapping[str, str] | None = None) -> list[AnchorSink]:
    env = os.environ if env is None else env
    names = [n.strip().lower() for n in env.get("GAWE_ANCHOR_SINKS", "").split(",") if n.strip()]
    if len(set(names)) != len(names):
        raise AnchorConfigError("GAWE_ANCHOR_SINKS lists a sink twice")
    directory = Path(env.get("GAWE_ANCHOR_DIR", "").strip() or DEFAULT_DIR)
    sinks: list[AnchorSink] = []
    for name in names:
        if name == "file":
            sinks.append(FileAnchorSink(directory / "anchors.jsonl"))
        elif name == "rfc3161":
            sinks.append(_timestamp_sink(directory, env))
        else:
            raise AnchorConfigError(f"GAWE_ANCHOR_SINKS: unknown sink {name!r} ({_AVAILABLE})")
    return sinks


def _timestamp_sink(directory: Path, env: Mapping[str, str]) -> AnchorSink:
    try:
        from gawe.anchoring.rfc3161 import AUTHORITIES, DEFAULT_AUTHORITY, Rfc3161AnchorSink
    except ImportError as exc:
        raise AnchorConfigError(
            'the rfc3161 sink needs the anchor extra: pip install -e ".[anchor]"'
        ) from exc
    authority = env.get("GAWE_ANCHOR_TSA", "").strip().lower() or DEFAULT_AUTHORITY
    if authority not in AUTHORITIES:
        known = ", ".join(sorted(AUTHORITIES))
        raise AnchorConfigError(f"GAWE_ANCHOR_TSA: unknown authority {authority!r} ({known})")
    return Rfc3161AnchorSink(directory / "anchors-rfc3161.jsonl", authority=authority)
