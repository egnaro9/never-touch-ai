"""The endpoints around the sweep: health, run ingest/list, compare, suite, /score's gate.

/sweep/expand is the interesting half of this server; everything here is the
plumbing that has to hold for the interesting half to be worth anything. A run
that does not come back is an experiment that did not happen, a /compare that
silently mis-aggregates is a leaderboard that ranks the wrong config, and /score
is the one endpoint that runs model-generated code on the machine -- so its gate
gets tested like a lock, not like a feature.

Ingest and clear both touch one process-wide store (`server._RUNS`), so every
test that cares about run state goes through `clean_store` rather than assuming
it starts empty.
"""
import pytest
from fastapi.testclient import TestClient

import server
from conftest import expand


def _run(config_name="cfg-a", *, score=None, ok=True, latency=120, cost=0.002, task="t1"):
    """A minimal valid RunRecord. Note the shape has nowhere to put a key."""
    return {
        "config_name": config_name,
        "task": task,
        "steps": [
            {
                "role": "worker",
                "model": "claude-sonnet-5",
                "latency_ms": latency,
                "input_tokens": 40,
                "output_tokens": 90,
                "cost_usd": cost,
            }
        ],
        "total_latency_ms": latency,
        "total_cost_usd": cost,
        "ok": ok,
        "score": score,
    }


@pytest.fixture
def clean_store(client):
    """Isolate the shared in-memory store on both sides of a test.

    Clearing on the way out matters as much as on the way in: leftover runs from
    this file would otherwise show up in another file's /compare or /health count
    and produce a failure that has nothing to do with the code under test.
    """
    client.delete("/runs")
    yield client
    client.delete("/runs")


# ---------------------------------------------------------------------------
# /health -- the liveness probe, and the only cheap window into store size.
# ---------------------------------------------------------------------------


def test_health_reports_ok_and_live_run_count(clean_store):
    """/health is what a deploy check and the browser's "is the backend up?" ping
    both read. If `runs_stored` stops tracking the real store, a user watching it
    cannot tell "my runs are being dropped" from "the backend is fine"."""
    c = clean_store
    assert c.get("/health").json() == {"ok": True, "runs_stored": 0}

    c.post("/runs", json=_run())
    c.post("/runs", json=_run("cfg-b"))

    assert c.get("/health").json() == {"ok": True, "runs_stored": 2}


# ---------------------------------------------------------------------------
# POST + GET /runs -- the browser hands results back; nothing else persists them.
# ---------------------------------------------------------------------------


def test_posted_run_comes_back_from_get_runs(clean_store):
    """The round trip IS the product: the browser runs the sweep with the user's
    key and posts sanitized traces here. If ingest accepts but the record never
    reappears, the whole run is lost and nothing in the UI says so -- POST still
    answered 200."""
    c = clean_store
    posted = _run("cfg-a", score=0.75, latency=310, cost=0.0042)

    r = c.post("/runs", json=posted)
    assert r.status_code == 200
    run_id = r.json()["stored"]
    assert run_id, "ingest must hand back the id it stored under"

    listed = c.get("/runs").json()
    assert listed["count"] == 1
    (got,) = listed["runs"]

    assert got["id"] == run_id
    assert isinstance(got["ts"], float), "the ts is what /runs orders by; it must be stored"
    for field in ("config_name", "task", "total_latency_ms", "total_cost_usd", "ok", "score"):
        assert got[field] == posted[field], f"ingest mangled '{field}' on the way in"
    assert got["steps"][0]["role"] == "worker"
    assert got["steps"][0]["cost_usd"] == 0.0042


def test_runs_are_listed_newest_first(clean_store):
    """The UI shows the tail of this list. Reverse the order and the "latest run"
    a user reads is actually the oldest one, which is the single most misleading
    way this endpoint can fail -- it looks like it works."""
    c = clean_store
    for name in ("first", "second", "third"):
        c.post("/runs", json=_run(name))

    runs = c.get("/runs").json()["runs"]
    assert [r["config_name"] for r in runs] == ["third", "second", "first"]
    assert all(runs[i]["ts"] >= runs[i + 1]["ts"] for i in range(len(runs) - 1))


def test_delete_runs_clears_the_store_and_reports_how_many(clean_store):
    """"Clear results" between sweeps has to actually clear, or the next /compare
    silently averages the old sweep in with the new one and every row is wrong."""
    c = clean_store
    c.post("/runs", json=_run("cfg-a"))
    c.post("/runs", json=_run("cfg-b"))

    assert c.delete("/runs").json() == {"cleared": 2}
    assert c.get("/runs").json() == {"count": 0, "runs": []}
    assert c.delete("/runs").json() == {"cleared": 0}, "clearing an empty store is not an error"


@pytest.mark.xfail(
    reason="SUSPECTED DEFECT: /runs validates with RunRecord.model_validate() inside the "
           "handler, so a pydantic ValidationError escapes uncaught and FastAPI turns it "
           "into a 500 instead of a 422. The browser posts these traces; a schema drift on "
           "either side should tell the caller what field is wrong, not read as a backend "
           "outage. Fix: wrap model_validate and re-raise as HTTPException(422, e.errors()).",
    strict=False,
)
def test_malformed_run_body_is_a_client_error_not_a_server_error():
    """A run trace missing required fields is the browser's bug, and the caller can
    only fix it if the response says which field. A 500 hides that, and a 500 is
    also what monitoring pages someone for -- wrong signal, wrong human."""
    # raise_server_exceptions=False so an uncaught handler exception is observed as
    # the 500 the real server would send, rather than blowing up the test itself.
    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.post("/runs", json={"config_name": "cfg-a"})  # missing task/steps/totals
    assert r.status_code == 422, f"expected a client error, got {r.status_code}"


# ---------------------------------------------------------------------------
# Extra fields -- the browser must not be able to smuggle a field the server keeps.
# ---------------------------------------------------------------------------


def test_unknown_fields_on_a_posted_config_are_ignored(client, base_cfg):
    """Load-bearing for the never-touch promise. The schema is the whole contract
    with the browser: anything not declared here must be dropped, not stored and
    not echoed. If pydantic's model config ever flips to `extra="allow"`, a field
    the guard never learned to look at rides in and comes back out of /runs."""
    smuggled = dict(base_cfg)
    smuggled["totally_unknown"] = "should be dropped"
    smuggled["roles"] = {
        "planner": {**base_cfg["roles"]["planner"], "temperature": 0.9},
        "worker": base_cfg["roles"]["worker"],
    }

    status, payload = expand(client, smuggled)
    assert status == 200, "an unknown field must be ignored, not rejected"

    (cfg,) = payload["configs"]
    assert "totally_unknown" not in cfg
    assert "temperature" not in cfg["roles"]["planner"]
    # ...and the declared neighbours still survive the drop.
    assert cfg["roles"]["planner"]["model"] == base_cfg["roles"]["planner"]["model"]


def test_unknown_fields_on_a_posted_run_are_ignored(clean_store):
    """Same contract on the ingest side, and this is the side that persists.
    A kept-but-undeclared field would be stored verbatim and served back out of
    GET /runs to every caller -- exactly the leak the credential guard exists to
    prevent, just through a field name the guard's regex never matched."""
    c = clean_store
    body = _run("cfg-a")
    body["client_note"] = "arbitrary browser-side field"
    body["steps"][0]["debug_headers"] = {"anything": "at all"}

    assert c.post("/runs", json=body).status_code == 200

    (stored,) = c.get("/runs").json()["runs"]
    assert "client_note" not in stored
    assert "debug_headers" not in stored["steps"][0]
    # The record is exactly the declared shape plus the two fields ingest adds.
    assert set(stored) == {
        "id", "ts", "config_name", "task", "steps",
        "total_latency_ms", "total_cost_usd", "ok", "score",
    }


# ---------------------------------------------------------------------------
# /compare -- the leaderboard. Getting the arithmetic wrong picks the wrong config.
# ---------------------------------------------------------------------------


def test_compare_is_empty_before_anything_runs(clean_store):
    """The UI renders this straight into a table. An exception or a missing key on
    an empty store means a fresh page load is a broken page load."""
    assert clean_store.get("/compare").json() == {"configs": []}


def test_compare_aggregates_every_run_of_a_config(clean_store):
    """This is the number a user spends money on. `runs_per_config` exists because
    one sample is noise, so the aggregate has to cover ALL of a config's runs --
    dropping one (or counting a failed run as a success) is how a worse-but-luckier
    config wins the comparison."""
    c = clean_store
    c.post("/runs", json=_run("cfg-a", score=0.5, ok=True, latency=100, cost=0.001))
    c.post("/runs", json=_run("cfg-a", score=1.0, ok=False, latency=300, cost=0.003))

    (row,) = c.get("/compare").json()["configs"]
    assert row["config_name"] == "cfg-a"
    assert row["n"] == 2
    assert row["mean_score"] == 0.75
    assert row["mean_latency_ms"] == 200
    assert row["mean_cost_usd"] == 0.002
    assert row["success_rate"] == 0.5, "ok=False must pull the success rate down"


def test_compare_ignores_unscored_runs_when_averaging(clean_store):
    """`score` is optional -- a run posted before the scoring pass has None. Folding
    that None in as a zero would make a half-scored config look bad for no reason;
    it must average only what was actually scored."""
    c = clean_store
    c.post("/runs", json=_run("cfg-a", score=1.0))
    c.post("/runs", json=_run("cfg-a", score=None))

    (row,) = c.get("/compare").json()["configs"]
    assert row["n"] == 2, "an unscored run still counts as a run"
    assert row["mean_score"] == 1.0

    c.post("/runs", json=_run("cfg-b", score=None))
    rows = {r["config_name"]: r for r in c.get("/compare").json()["configs"]}
    assert rows["cfg-b"]["mean_score"] is None, "no scores at all must stay None, not 0.0"


def test_compare_ranks_best_score_first_then_cheapest(clean_store):
    """Row order is the recommendation. "Correct first, then cheap" is the whole
    thesis -- if cost ever outranks score, the tool starts recommending the config
    that is worse at the job because it saved a tenth of a cent."""
    c = clean_store
    c.post("/runs", json=_run("cheap-and-wrong", score=0.2, cost=0.0001))
    c.post("/runs", json=_run("pricey-and-right", score=0.9, cost=0.05))
    c.post("/runs", json=_run("also-right-but-cheap", score=0.9, cost=0.01))
    c.post("/runs", json=_run("never-scored", score=None, cost=0.0001))

    order = [r["config_name"] for r in c.get("/compare").json()["configs"]]
    assert order[:2] == ["also-right-but-cheap", "pricey-and-right"], \
        "equal scores must break the tie on cost, cheapest first"
    assert order[2] == "cheap-and-wrong"
    assert order[-1] == "never-scored", \
        "an unranked config must not be presented above a measured one"


# ---------------------------------------------------------------------------
# /score -- the only endpoint that executes code. The gate is the safety property.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_exec(monkeypatch):
    """Replace the code executor with a recorder.

    Two reasons. First, it makes "was code executed?" directly observable instead
    of inferred. Second, it keeps this file hermetic: the real path shells out to
    Docker (pulling an image) or to a bare subprocess, neither of which belongs in
    a unit test.
    """
    calls = []

    def _recorder(candidate, test, timeout=8):
        calls.append((candidate, test))
        return True, ""

    monkeypatch.setattr(server, "_run_python", _recorder)
    return calls


_PY_CHECK = {
    "output": "```python\ndef f():\n    return 1\n```",
    "checks": [{"kind": "python", "test": "assert f() == 1", "label": "f"}],
}


def test_score_refuses_without_a_token(client, no_exec):
    """/score runs model-generated code in a subprocess. Ungated, any page the user
    happens to have open can POST Python to localhost and have it run on their
    machine. The 401 is the lock; `no_exec` proves nothing ran behind it."""
    r = client.post("/score", json=_PY_CHECK)
    assert r.status_code == 401
    assert no_exec == [], "code must not execute for an unauthenticated caller"


def test_score_refuses_a_wrong_token(client, no_exec):
    """A guess, a stale token from a previous boot, or an empty header must all
    land the same way. A partial match must not be accepted either -- the compare
    is constant-time hmac precisely so the token cannot be discovered one byte at
    a time."""
    for bad in ("", "wrong", server._TOKEN[:-1], server._TOKEN + "x"):
        r = client.post("/score", json=_PY_CHECK, headers={"x-ntai-token": bad})
        assert r.status_code == 401, f"token {bad!r} must not open the gate"
        assert server._TOKEN not in r.text, "the refusal must not leak the real token"
    assert no_exec == []


def test_score_runs_the_check_with_the_right_token(client, no_exec):
    """The other half of the lock: it has to open. If the gate rejects the real
    token the tool cannot score anything at all, and this also proves the negative
    tests above are testing a gate rather than a permanently dead endpoint."""
    r = client.post("/score", json=_PY_CHECK, headers={"x-ntai-token": server._TOKEN})
    assert r.status_code == 200

    body = r.json()
    assert body["passed"] == 1 and body["total"] == 1
    assert body["score"] == 1.0
    assert body["results"][0]["engine"] == "local-exec", \
        "execution is local by design; gradecore is pure and does not run code"

    assert len(no_exec) == 1, "an authenticated python check must reach the executor"
    candidate, test = no_exec[0]
    assert "```" not in candidate, "the fence must be stripped before the code is run"
    assert test == "assert f() == 1"


def test_score_text_check_needs_no_execution(client, no_exec):
    """Text predicates are the cheap path and must stay off the executor entirely --
    if a `contains` check ever started spawning a subprocess, grading a suite would
    go from milliseconds to minutes and the sandbox warning would apply to output
    nobody meant to run."""
    r = client.post(
        "/score",
        json={"output": "the answer is 42", "checks": [{"kind": "contains", "pattern": "42"}]},
        headers={"x-ntai-token": server._TOKEN},
    )
    assert r.status_code == 200
    assert r.json()["results"][0]["ok"] is True
    assert r.json()["results"][0]["engine"] in ("gradecore", "fallback"), \
        "gradecore is an optional dep; absent, the same semantics must degrade to the fallback"
    assert no_exec == []


# ---------------------------------------------------------------------------
# /suite -- the curated tasks. The asset that makes this an evaluator.
# ---------------------------------------------------------------------------


def test_suite_serves_the_coding_suite_with_a_stable_task_count(client):
    """Scores are only comparable across sweeps if the suite is the same suite.
    Silently losing tasks (a truncated file, a bad edit) would move every score
    without moving anything a user can see, so the count is pinned deliberately:
    change it only when the suite really changed."""
    r = client.get("/suite")
    assert r.status_code == 200

    suite = r.json()
    assert suite["name"] == "coding-starter-v2"
    assert len(suite["tasks"]) == 20


def test_suite_task_ids_are_unique_and_every_task_is_gradeable(client):
    """Results are keyed by task id downstream, so a duplicate id means one task's
    result overwrites another's and the leaderboard quietly grades 19 tasks while
    claiming 20. A task with no checks is worse: it can never fail, so it inflates
    every config's score equally and measures nothing."""
    tasks = client.get("/suite").json()["tasks"]

    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == len(ids), f"duplicate task ids: {sorted({i for i in ids if ids.count(i) > 1})}"
    assert all(ids), "every task needs an id to key its results by"

    for t in tasks:
        assert t["checks"], f"task '{t['id']}' has no checks -- it can never fail"
        for chk in t["checks"]:
            assert chk.get("kind"), f"task '{t['id']}' has a check with no kind"
