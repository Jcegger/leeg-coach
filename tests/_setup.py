"""Shared test setup: import live.py without writing to tools/leeg.log or data/."""
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import live  # noqa: E402

live.log.handlers.clear()
live.log.addHandler(logging.NullHandler())
live.GAMES_LOG = Path(tempfile.mkdtemp()) / 'games.jsonl'
