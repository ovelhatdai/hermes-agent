#!/usr/bin/env python3
"""Cron entrypoint for SPEC-077 Compozy watcher."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    explicit = (os.getenv("HERMES_AGENT_REPO") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def main() -> int:
    repo_root = _repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from gateway.platforms._custom.compozy_watcher import main as watcher_main

    exit_code = watcher_main()
    if exit_code == 0:
        print('{"wakeAgent": false}')
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
