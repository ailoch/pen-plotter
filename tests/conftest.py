"""Shared pytest fixtures.

The pipeline reads config-relative paths (gcode templates) with plain relative
paths, so every test runs with the repo root as the working directory.
"""
import os
import sys
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = "config/bambu_p1s_config.json"

# lib/ is imported as "lib.x", so the repo root has to be importable
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture(autouse=True)
def repoRoot(monkeypatch):
    """Run every test from the repo root, whatever directory pytest was invoked from."""
    monkeypatch.chdir(REPO_ROOT)


@pytest.fixture
def settings():
    """A Settings loaded from the real P1S config.

    Deliberately the real config rather than hand-built defaults: these tests are
    checking the pipeline as actually shipped, so a config change that breaks
    coverage should fail here.
    """
    from lib.settings import Settings
    s = Settings()
    s.initFromJson(CONFIG)
    s.profiling = False
    return s
