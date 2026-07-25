"""Shared fixtures.

The backend is the *second* implementation of the sweep: the browser expands a
config and so does the server, and a mismatch between them is the signal that
one of them drifted. These tests pin the server side of that oracle, so the
cross-check keeps meaning something.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# server.py sits at the repo root next to this tests/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(server.app)


@pytest.fixture
def base_cfg() -> dict:
    """A minimal two-role chain. Enough to be valid, small enough to reason about."""
    return {
        "name": "t",
        "roles": {
            "planner": {"model": "claude-opus-4-8", "system": "plan"},
            "worker": {"model": "claude-sonnet-5", "system": "do"},
        },
        "pipeline": [],
        "graph": {"planner": [], "worker": ["planner"]},
    }


def expand(client: TestClient, base: dict, axes: list | None = None):
    """POST /sweep/expand and return (status, payload)."""
    r = client.post("/sweep/expand", json={"base": base, "axes": axes or []})
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)
