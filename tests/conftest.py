import os

# The garment-facts step adds a Gemini text call before each image. Tests that count Gemini calls predate it,
# so it is off by default here; tests of the step turn it on explicitly.
os.environ.setdefault("GARMENT_FACTS_ENABLED", "false")
# The paid plans' final 4K pass is one more Gemini call per look; tests of it turn it on explicitly.
os.environ.setdefault("PAID_IMAGE_SIZE", "1K")

import pytest


@pytest.fixture(autouse=True)
def _no_container_memory_limit(monkeypatch):
    """4K tests must not depend on the test machine's memory; tests of the small-server fallback set it."""
    from app import tryon

    monkeypatch.setattr(tryon, "_memory_limit", lambda: None)
