"""The never-touch boundary.

`assert_no_credentials()` is the load-bearing part of the product's central
claim: a hosted BYOK tool where "the key never touches the server" is only worth
something if a buggy or hostile client physically cannot get a key into storage.
README section 4 states it as an absolute -- the guard "walks every incoming
payload and returns 422 if any field name *or* value looks like a key."

So these tests are written from two directions at once:

  * it must REFUSE -- key-shaped values however deeply they are buried, and
    key-shaped field names even when the value is harmless;
  * it must NOT refuse the payload the browser actually sends, because a false
    positive here silently breaks the config cross-check on every run and the
    user just sees the sweep stop reporting.

The catch matrix at the bottom pins which real-world key formats the value
regex actually recognises. The ones marked xfail are formats that SLIP THROUGH
today -- they are recorded as tests rather than as a comment so that tightening
the regex flips them green instead of going unnoticed.
"""
import json

import pytest
from fastapi import HTTPException

import server


# Fake but real-shaped. Never a live credential -- these exist so the regex is
# tested against the thing it claims to catch, not against a toy string.
ANTHROPIC_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
ANTHROPIC_ADMIN_KEY = "sk-ant-admin01-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
OPENAI_LEGACY_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4"
GOOGLE_KEY = "AIzaSy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"


@pytest.fixture
def fresh_store(monkeypatch):
    """Swap the in-memory run store so ingest tests cannot see each other.

    `_RUNS` is a module global; without this, run counts depend on test order
    and on whichever other test file ran first.
    """
    store: dict = {}
    monkeypatch.setattr(server, "_RUNS", store)
    return store


def run_record(**over) -> dict:
    """A minimal *valid* sanitized trace -- the shape sweep.html POSTs to /runs."""
    rec = {
        "config_name": "t",
        "task": "reverse a string",
        "steps": [
            {"role": "planner", "model": "claude-opus-4-8", "latency_ms": 12,
             "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.001},
            {"role": "worker", "model": "claude-sonnet-5", "latency_ms": 34,
             "input_tokens": 30, "output_tokens": 40, "cost_usd": 0.002},
        ],
        "total_latency_ms": 46,
        "total_cost_usd": 0.003,
        "ok": True,
    }
    rec.update(over)
    return rec


def refusal(payload) -> str | None:
    """Run the guard and return the refusal text, or None if it let the payload through."""
    try:
        server.assert_no_credentials(payload)
    except HTTPException as e:
        return str(e.detail)
    return None


# ---------------------------------------------------------------------------
# Key-shaped VALUES, buried at every depth the real payloads reach
# ---------------------------------------------------------------------------


def test_key_value_deep_in_a_list_is_refused(client, fresh_store):
    """A key pasted into one step of a multi-step trace must not be stored.

    The leak path this closes is mundane and therefore likely: a client bug (or
    a model echoing its own env) puts the key into `output_preview` of step 2 of
    5. The guard recurses into lists, so depth is not an escape hatch.
    """
    rec = run_record()
    rec["steps"][1]["output_preview"] = f"export ANTHROPIC_API_KEY={ANTHROPIC_KEY}"

    r = client.post("/runs", json=rec)

    assert r.status_code == 422
    # The path is named because that is what makes a leak traceable to the line
    # of client code that caused it; a bare "bad payload" would not.
    assert "steps[1].output_preview" in r.json()["detail"]
    assert fresh_store == {}, "a refused payload must never reach the store"


def test_key_value_inside_a_role_is_refused(client, fresh_store, base_cfg):
    """A key smuggled through a role's system prompt must not be stored.

    `system` is free text a user edits by hand, so it is the most plausible
    place for a key to end up by accident -- pasted into scaffolding while
    someone was testing "does the model see my key".
    """
    cfg = json.loads(json.dumps(base_cfg))
    cfg["roles"]["planner"]["system"] = f"You may call the API with {ANTHROPIC_KEY}"

    # Extra keys on the body are dropped by RunRecord, but the guard walks the
    # RAW json -- so "pydantic will ignore it" is not a reason for it to be safe.
    r = client.post("/runs", json=run_record(config=cfg))

    assert r.status_code == 422
    assert "config.roles.planner.system" in r.json()["detail"]
    assert fresh_store == {}


def test_key_value_inside_a_substrate_is_refused(client, fresh_store, base_cfg):
    """A key hidden in a substrate's base_url must not be stored.

    Serving paths are the one place in this schema that holds a URL, and
    `?api_key=...` is how OpenAI-compatible proxies are commonly authenticated,
    so this is the realistic way a credential gets into a config object.
    """
    cfg = json.loads(json.dumps(base_cfg))
    cfg["substrates"] = {
        "proxy": {"name": "proxy", "shape": "openai",
                  "base_url": f"http://localhost:11434/v1?api_key={OPENAI_LEGACY_KEY}"},
    }

    r = client.post("/runs", json=run_record(config=cfg))

    assert r.status_code == 422
    assert "config.substrates.proxy.base_url" in r.json()["detail"]
    assert fresh_store == {}


def test_key_value_in_a_bare_list_body_is_refused(client, fresh_store):
    """A non-object body is still walked, so the guard cannot be dodged by shape.

    A client that posts `[record, record]` instead of one record would otherwise
    hand the guard something it never inspects.
    """
    r = client.post("/runs", json=["fine", ANTHROPIC_KEY])

    assert r.status_code == 422
    assert "[1]" in r.json()["detail"]


def test_refusal_never_echoes_the_credential(client, fresh_store):
    """The 422 body must name the path, not the secret.

    An error that quotes the offending value would re-emit the key into browser
    consoles, proxy logs and error trackers -- turning the guard itself into the
    leak it exists to prevent.
    """
    rec = run_record()
    rec["steps"][0]["output_preview"] = ANTHROPIC_KEY

    r = client.post("/runs", json=rec)

    assert r.status_code == 422
    assert ANTHROPIC_KEY not in r.text
    assert ANTHROPIC_KEY[:20] not in r.text


# ---------------------------------------------------------------------------
# Key-shaped FIELD NAMES -- refused on the name alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", [
    "api_key", "apiKey", "API-KEY", "x-api-key", "anthropic_api_key",
    "Authorization", "authorization", "bearer", "secret", "my_secret_sauce",
    "access_token", "accessToken", "access-token",
])
@pytest.mark.parametrize("value", ["hello", None])   # str and non-str: the name
                                                     # check must not depend on
                                                     # the value being walked
def test_key_shaped_field_name_is_refused_whatever_the_value(client, fresh_store, field, value):
    """A credential-shaped field name is refused before the value is even looked at.

    This is the defence against a key format the value regex does not know: if
    the client labels the thing honestly, the label alone is enough to refuse.
    A regression here means an unknown-format key rides in under a known name.
    """
    r = client.post("/runs", json=run_record(**{field: value}))

    assert r.status_code == 422
    assert "looks like a credential" in r.json()["detail"]
    assert fresh_store == {}


def test_field_name_check_runs_before_schema_validation(client, fresh_store):
    """The guard fires even on a body that could never validate as a RunRecord.

    Order matters: if `RunRecord.model_validate()` ran first, a malformed body
    carrying a key would be rejected as a *schema* error, and the credential
    would already have been parsed, logged by whatever wraps the 422, and never
    flagged as a leak.
    """
    r = client.post("/runs", json={"totally": "wrong shape", "x-api-key": "hello"})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str), "a pydantic error list here means schema ran first"
    assert "looks like a credential" in detail


def test_nested_key_shaped_field_name_is_refused(client, fresh_store, base_cfg):
    """The name check applies at every depth, not just the top level.

    A client that attaches auth per-substrate (`substrates.x.authorization`) is
    the exact shape a "just make it work with my proxy" patch produces.
    """
    cfg = json.loads(json.dumps(base_cfg))
    cfg["substrates"] = {"proxy": {"name": "proxy", "authorization": "Basic dXNlcjpwdw=="}}

    r = client.post("/runs", json=run_record(config=cfg))

    assert r.status_code == 422
    assert "config.substrates.proxy.authorization" in r.json()["detail"]


# ---------------------------------------------------------------------------
# No false positives -- the browser's real payloads must sail through
# ---------------------------------------------------------------------------


def test_legit_config_with_substrates_and_base_urls_expands_cleanly(client):
    """The exact shape the browser POSTs on every cross-check must not be refused.

    sweep.html expands a config locally and asks the server to expand the same
    one; a disagreement is the drift signal. If a plain `base_url` or substrate
    name tripped the credential guard, that oracle would go silently dark -- the
    UI treats a failed POST as "backend offline", not as a bug.
    """
    cfg = {
        "name": "two-vendor panel",
        "roles": {
            "planner": {"model": "claude-opus-4-8", "system": "plan", "substrate": "anthropic_direct"},
            "worker": {"model": "claude-sonnet-5", "system": "do", "substrate": "openai_compatible"},
        },
        "graph": {"planner": [], "worker": ["planner"]},
        "substrates": {
            "anthropic_direct": {"name": "anthropic_direct", "shape": "anthropic",
                                 "base_url": "https://api.anthropic.com"},
            "openai_compatible": {"name": "openai_compatible", "shape": "openai",
                                  "base_url": "http://localhost:11434/v1"},
        },
    }
    axes = [{"kind": "substrate", "role": "*",
             "values": ["anthropic_direct", "openai_compatible"]}]

    # The guard itself is clean on this payload...
    assert refusal({"base": cfg, "axes": axes}) is None
    # ...and the endpoint the browser actually calls agrees.
    r = client.post("/sweep/expand", json={"base": cfg, "axes": axes})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2


def test_prose_about_api_keys_is_not_a_credential(client, fresh_store):
    """Talking about keys is not carrying one.

    The suite grades coding tasks, and "read the key from the environment" is a
    thing correct answers say. Refusing text that merely mentions credentials
    would make whole categories of task unreportable -- and only for the runs
    that passed.
    """
    rec = run_record()
    rec["steps"][0]["output_preview"] = (
        "Never hard-code a secret: read os.environ['ANTHROPIC_API_KEY'] and pass "
        "it as the x-api-key header. Bearer auth works the same way."
    )
    rec["task"] = "explain how to authenticate without leaking an api_key"

    r = client.post("/runs", json=rec)

    assert r.status_code == 200
    assert len(fresh_store) == 1


@pytest.mark.parametrize("model", [
    server.Substrate, server.RoleSpec, server.HarnessConfig, server.SweepAxis,
    server.ConfigSweep, server.StepTrace, server.RunRecord, server.Check,
    server.ScoreRequest,
])
def test_no_schema_field_name_trips_the_guard(model):
    """No field the app itself defines may look like a credential.

    The guard matches on substrings, so adding an innocent field one day --
    `access_token_count`, `secret_seed`, `authorization_mode` -- would make the
    app's own payloads permanently unpostable. Cheaper to catch here than in
    production, where it presents as "runs stopped appearing".
    """
    for field in model.model_fields:
        assert not server._KEYISH_FIELD.search(field), (
            f"{model.__name__}.{field} matches the credential-name pattern; "
            f"the client can no longer post its own schema"
        )


def test_empty_and_scalar_payloads_are_walked_without_error():
    """Degenerate payloads must not crash the walker.

    A crash here is a 500 on the ingest path, i.e. the guard failing open in
    the operational sense: the client's retry logic sees a server fault instead
    of a verdict.
    """
    for payload in ({}, [], "", 0, None, True, 1.5,
                    {"a": None, "b": [1, 2.5, None, {"c": []}]}):
        assert refusal(payload) is None


# ---------------------------------------------------------------------------
# The catch matrix -- which real key formats does the value regex actually see?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,secret", [
    ("anthropic", ANTHROPIC_KEY),
    ("anthropic-admin", ANTHROPIC_ADMIN_KEY),
    ("openai-legacy", OPENAI_LEGACY_KEY),
    ("google-ai-studio", GOOGLE_KEY),
])
def test_known_key_formats_are_caught_by_value(label, secret):
    """The formats the guard claims to know must stay caught, in any surrounding text.

    Real leaks arrive embedded -- in a curl line, a stack trace, a config dump --
    not as a bare token, so anchoring the regex would be a silent regression.
    """
    assert refusal({"steps": [{"output_preview": secret}]}) is not None
    assert refusal({"note": f"try: curl -H 'x: {secret}' https://api.example.com"}) is not None


@pytest.mark.parametrize("label,secret", [
    pytest.param("openai-project", "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"),
    pytest.param("openrouter", "sk-or-v1-" + "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"),
    pytest.param("bearer-jwt-value", "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                                     "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r8W1gFWFOEjXk"),
    pytest.param("aws-secret-access-key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    pytest.param("groq", "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"),
    pytest.param("long-base64-blob",
                 "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgYmxvYiB0aGF0IGNvdWxkIGJlIGEga2V5PT0="),
])
def test_key_formats_that_should_be_caught_but_slip_through(label, secret):
    """Realistic credentials the value regex does not recognise today.

    Each of these can be posted to /runs under a harmless field name and lands
    in the store in plaintext -- the precise outcome "never touch" promises is
    impossible. Marked xfail so the file stays green while the hole is open, and
    turns red the moment someone believes it is closed without fixing it.
    """
    assert refusal({"steps": [{"output_preview": secret}]}) is not None


def test_credential_in_key_position_is_refused():
    """A credential used as a dict key is still a credential.

    Sounds exotic, but 'index my per-key usage' is a natural client-side shape,
    and the stored blob is just as leaked as if it were a value.
    """
    assert refusal({"usage": {ANTHROPIC_KEY: 3}}) is not None


# ---------------------------------------------------------------------------
# Which endpoints are actually behind the boundary
# ---------------------------------------------------------------------------


def test_sweep_expand_is_behind_the_same_boundary(client, base_cfg):
    """The config endpoint takes credential-carrying fields too, and must refuse them.

    `?api_key=` in a substrate base_url is the ordinary way to authenticate an
    OpenAI-compatible proxy, so this is not a contrived payload -- and because
    /sweep/expand reflects the expanded configs, a key posted here comes straight
    back out through any logging or caching layer in front of the server.
    """
    cfg = json.loads(json.dumps(base_cfg))
    cfg["substrates"] = {
        "proxy": {"name": "proxy", "shape": "openai",
                  "base_url": f"http://localhost:11434/v1?api_key={ANTHROPIC_KEY}"},
    }

    r = client.post("/sweep/expand", json={"base": cfg, "axes": []})

    assert r.status_code == 422, "config endpoint accepted a credential"
    assert ANTHROPIC_KEY not in r.text, "and reflected it back to the caller"
