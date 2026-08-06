from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from accesspulse.runtime import AccessPulseRuntime  # noqa: E402

BENCH_SWEEP = dict(
    languages=["en", "fr", "de"],
    territories=["FR", "DE", "US"],
    player_versions=["ctv-9.3.1", "ctv-9.4.0", "web-4.12.0"],
)


@pytest.fixture
async def runtime():
    rt = AccessPulseRuntime(db_prefix="test")
    await rt.connect()
    yield rt
    await rt.aclose()


@pytest.fixture
def sim():
    from accesspulse.simulator import MediaSimulator

    return MediaSimulator(seed=1234)
