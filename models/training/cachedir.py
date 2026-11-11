"""Where the long-running scripts keep their caches, logs and --small outputs.

Resolution (first that applies):
  1. the script's own CLI flag (--cache DIR; it overrides only the simulation/CV cache directory),
  2. environment variable RESONANCE_CACHE_DIR: a base directory, each script uses <base>/<script name>/,
  3. <repo>/.cache/<script name>/   (git-ignored; delete it any time, the scripts re-simulate what is missing).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = "RESONANCE_CACHE_DIR"


def base():
    v = os.environ.get(ENV)
    return Path(v) if v else ROOT / ".cache"


def script_dir(name):
    return base() / name
