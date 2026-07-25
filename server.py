"""
AI Crash Test - backend
========================
This server NEVER sees an API key and NEVER makes a provider call.

Its only jobs:
  1. Define the canonical harness-config schema (Pydantic) -- the keystone
     that makes configs enumerable and comparable.
  2. Expand a sweep into concrete configs (the cartesian product of axes).
  3. Store the *sanitized* run traces the browser posts back.
  4. Serve stored runs, aggregated per config, for comparison.

A guard on the ingest endpoint rejects any payload that looks like it carries
a credential, so "never touch" is enforced at the boundary -- not just promised.

Run:  pip install "fastapi>=0.110" "uvicorn[standard]"
      uvicorn server:app --reload --port 8000

Requires Python 3.10+.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# ----------------------------------------------------------------------------
# Schema -- a harness config is DATA. Once configs are declarative you can
# enumerate combinations and compare them. This is the whole point.
# ----------------------------------------------------------------------------


class Substrate(BaseModel):
    """WHERE a model runs — the serving path behind a stable model id.

    A model id is not a system. The same id can be served from different
    endpoints, hardware, quantizations and point releases, and none of that is
    visible in the response: it comes back 200, well-formed, gradeable, and
    possibly worse. Holding the id fixed while varying this is the only way to
    see it. (The observation that the serving path is an unmeasured third
    variable — alongside the model and the grader — is ANP2 Network's.)
    """

    name: str                     # "anthropic-direct", "bedrock", "local-vllm"
    shape: str = "anthropic"      # wire format: "anthropic" | "openai"
    base_url: str = ""            # "" = the shape's default endpoint


class RoleSpec(BaseModel):
    """One role in the harness: a model bound to some scaffolding."""

    model: str                    # e.g. "claude-opus-4-8"
    system: str = ""              # the scaffolding / system prompt for this role
    max_tokens: int = Field(default=1024, ge=1, le=64000)
    substrate: Optional[str] = None   # name of a Substrate; None = harness default


class HarnessConfig(BaseModel):
    """
    A full harness configuration.

    `pipeline` is the topology: an ordered list of role names where each role's
    output feeds the next (manager -> worker is the canonical case). Kept linear
    for the skeleton; it generalizes to a DAG later without changing anything
    else here.
    """

    name: str
    roles: dict[str, RoleSpec]
    pipeline: list[str] = []                # sugar: a chain. compiles to `graph`
    graph: dict[str, list[str]] = {}        # role -> the roles feeding it
    substrates: dict[str, Substrate] = {}   # named serving paths this config may use
    runs_per_config: int = Field(default=3, ge=1, le=20)  # repeats for variance

    @field_validator("pipeline")
    @classmethod
    def _pipeline_roles_exist(cls, v: list[str], info: Any) -> list[str]:
        roles = info.data.get("roles", {})
        missing = [r for r in v if r not in roles]
        if missing:
            raise ValueError(f"pipeline references unknown roles: {missing}")
        return v

    def as_graph(self) -> dict[str, list[str]]:
        """The topology as a DAG, whichever way it was written.

        `pipeline: [a, b, c]` is the chain {a:[], b:[a], c:[b]} — kept because a
        chain is the common case and nobody should write a graph to express one.
        `graph` is the general form, and it is what makes shapes other than a
        line expressible at all: fan-out to parallel drafters, fan-in to a judge,
        a critic that sees both the plan and the draft.
        """
        if self.graph:
            return {k: list(v) for k, v in self.graph.items()}
        return {r: ([self.pipeline[i - 1]] if i else []) for i, r in enumerate(self.pipeline)}

    def order(self) -> list[str]:
        """Topological order, or a 422 naming the exact problem.

        Refuses cycles rather than looping forever, and refuses edges into roles
        that do not exist — both are config bugs that would otherwise surface as
        a confusing runtime failure after you have already paid for calls.
        """
        g = self.as_graph()
        if not g:
            raise HTTPException(422, "config has no topology: set `pipeline` or `graph`")
        for node, deps in g.items():
            if node not in self.roles:
                raise HTTPException(422, f"graph node '{node}' has no matching role")
            for d in deps:
                if d not in g:
                    raise HTTPException(422, f"'{node}' depends on '{d}', which is not in the graph")

        done: list[str] = []
        seen: set[str] = set()
        while len(done) < len(g):
            ready = [n for n, deps in g.items()
                     if n not in seen and all(d in seen for d in deps)]
            if not ready:
                stuck = sorted(set(g) - seen)
                raise HTTPException(422, f"graph has a cycle among: {stuck}")
            ready.sort()                      # deterministic order for reproducibility
            done.extend(ready)
            seen.update(ready)
        return done

    def terminals(self) -> list[str]:
        """Nodes nothing depends on — the harness's answer comes from here."""
        g = self.as_graph()
        fed = {d for deps in g.values() for d in deps}
        return sorted(n for n in g if n not in fed)


class SweepAxis(BaseModel):
    """One dimension of the sweep.

    Three kinds, because there are three genuinely different questions:

      role      — vary a field of one role. "Which model should plan?"
      topology  — vary the pipeline itself. "Do I even need a manager?"
                  values are pipelines: [["worker"], ["manager","worker"]]
      substrate — vary the serving path while the model id stays fixed.
                  "Is Sonnet-via-Bedrock the same thing as Sonnet-direct?"
                  values are substrate names; role="*" applies to every role.

    `kind` defaults to "role" so every config written before this still parses.
    """

    kind: str = "role"            # "role" | "topology" | "substrate"
    role: str = ""                # role kinds: which role; substrate: role or "*"
    field: str = ""               # role kind: "model" | "system" | "max_tokens"
    values: list[Any]

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in ("role", "topology", "substrate"):
            raise ValueError(f"axis kind must be role|topology|substrate, got '{v}'")
        return v


class ConfigSweep(BaseModel):
    """
    A base config plus axes to vary. `expand()` returns the cartesian product as
    concrete HarnessConfigs -- this is the "enumerate every combination" step
    that turns a manual A/B into a systematic sweep.
    """

    base: HarnessConfig
    axes: list[SweepAxis] = []

    def expand(self) -> list[HarnessConfig]:
        if not self.axes:
            return [self.base]
        out: list[HarnessConfig] = []
        for combo in itertools.product(*[ax.values for ax in self.axes]):
            cfg = self.base.model_copy(deep=True)
            label_bits = []
            for ax, val in zip(self.axes, combo):

                if ax.kind == "topology":
                    # A value is either a chain (list of roles) or a shape
                    # (dict of role -> its inputs). Both describe a topology;
                    # the dict form is the one that can branch.
                    if isinstance(val, dict):
                        if not val:
                            raise HTTPException(422, "topology graph cannot be empty")
                        missing = [r for r in val if r not in cfg.roles]
                        if missing:
                            raise HTTPException(422, f"topology graph references unknown roles: {missing}")
                        cfg.graph = {k: list(v) for k, v in val.items()}
                        cfg.pipeline = []
                        label_bits.append(_shape_label(cfg))
                    elif isinstance(val, list) and val:
                        missing = [r for r in val if r not in cfg.roles]
                        if missing:
                            raise HTTPException(422, f"topology '{val}' references unknown roles: {missing}")
                        cfg.pipeline = list(val)
                        cfg.graph = {}
                        label_bits.append("→".join(val))
                    else:
                        raise HTTPException(422, "topology values must be a non-empty chain or graph")

                elif ax.kind == "substrate":
                    if val not in cfg.substrates:
                        raise HTTPException(
                            422, f"substrate '{val}' is not defined in base.substrates "
                                 f"({sorted(cfg.substrates)})")
                    targets = list(cfg.roles) if ax.role in ("", "*") else [ax.role]
                    for r in targets:
                        if r not in cfg.roles:
                            raise HTTPException(422, f"substrate axis references unknown role '{r}'")
                        cfg.roles[r].substrate = val
                    label_bits.append(f"on={val}" if ax.role in ("", "*")
                                      else f"{ax.role}@{val}")

                else:                                   # kind == "role"
                    if ax.role not in cfg.roles:
                        raise HTTPException(422, f"sweep axis references unknown role '{ax.role}'")
                    setattr(cfg.roles[ax.role], ax.field, val)
                    label_bits.append(f"{ax.role}.{ax.field}={_short(val)}")

            cfg.name = f"{self.base.name} [{', '.join(label_bits)}]"
            out.append(cfg)
        return out


def _short(v: Any) -> str:
    s = str(v)
    return s if len(s) <= 24 else s[:21] + "..."


def _shape_label(cfg: "HarnessConfig") -> str:
    """A readable name for a topology, so a leaderboard row says what it ran.

    "planner→[draft_a|draft_b]→judge" beats "config 3".
    """
    g = cfg.as_graph()
    try:
        order = cfg.order()
    except HTTPException:
        return "invalid-graph"
    levels: list[list[str]] = []
    placed: set[str] = set()
    for _ in range(len(order)):
        tier = [n for n in order if n not in placed and all(d in placed for d in g[n])]
        if not tier:
            break
        levels.append(sorted(tier))
        placed.update(tier)
    return "→".join(t[0] if len(t) == 1 else "[" + "|".join(t) + "]" for t in levels)


# ----------------------------------------------------------------------------
# Sanitized run records -- what the BROWSER posts back after it has run a config
# with the user's key. Note there is no key field anywhere in this shape.
# ----------------------------------------------------------------------------


class StepTrace(BaseModel):
    role: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ok: bool = True
    error: Optional[str] = None
    output_preview: str = ""      # user's own content, not a secret; optional


class RunRecord(BaseModel):
    config_name: str
    task: str
    steps: list[StepTrace]
    total_latency_ms: int
    total_cost_usd: float
    ok: bool = True
    score: Optional[float] = None      # 0..1, correctness from the scoring pass


# ----------------------------------------------------------------------------
# The never-touch boundary guard. Crude but effective: reject anything that
# smells like a credential, by field name or by value pattern.
# ----------------------------------------------------------------------------

_KEYISH_FIELD = re.compile(r"(api[_-]?key|x-api-key|authorization|secret|bearer|access[_-]?token)", re.I)

# Vendor prefixes, then a generic high-entropy catch-all.
#
# The previous value pattern was `sk-[A-Za-z0-9]{32,}` -- 32 UNBROKEN
# alphanumerics after "sk-". OpenAI's current project keys are hyphen-segmented
# (`sk-proj-...`) and OpenRouter's are `sk-or-v1-...`, so the run stopped after
# four characters and both sailed straight through. Those are the formats a user
# of a BYOK tool actually holds, which made the guard weakest exactly where it
# mattered. Allowing [-_] inside the run fixes that whole family at once.
#
# "bearer" was in the FIELD-name pattern only, so a token parked under an
# innocent field name (note, task, output_preview) was stored verbatim.
_KEYISH_VALUE = re.compile(
    r"("
    r"sk-ant-[A-Za-z0-9\-_]{8,}"          # Anthropic
    r"|sk-[A-Za-z0-9\-_]{20,}"            # OpenAI incl. sk-proj-, OpenRouter sk-or-v1-
    r"|AIza[0-9A-Za-z\-_]{20,}"           # Google
    r"|gsk_[A-Za-z0-9]{20,}"              # Groq
    r"|xai-[A-Za-z0-9]{20,}"              # xAI
    r"|hf_[A-Za-z0-9]{20,}"               # Hugging Face
    r"|AKIA[0-9A-Z]{16}"                  # AWS access key id
    r"|ASIA[0-9A-Z]{16}"                  # AWS temporary
    r"|Bearer\s+[A-Za-z0-9\-._~+/]{20,}"  # any bearer token, by its own label
    r"|eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"   # JWT
    r")"
)

# The catch-all. A guard that only knows named vendors is an allowlist, and the
# next provider to launch defeats it. Anything long and random-looking in a
# config is not something this server has any reason to accept.
_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")


def _looks_random(s: str) -> bool:
    """True for a long blob with the character mix of a secret, not of prose.

    Deliberately conservative: real fields here are prompts, model ids, role
    names and URLs. Those are short, or contain spaces, or repeat words. A
    40-plus character run with mixed case AND digits and no spaces is not any
    of those.
    """
    for m in _HIGH_ENTROPY.finditer(s):
        blob = m.group(0)
        if " " in blob:
            continue
        has_lower = any(c.islower() for c in blob)
        has_upper = any(c.isupper() for c in blob)
        has_digit = any(c.isdigit() for c in blob)
        if has_lower and has_upper and has_digit:
            return True
    return False


def assert_no_credentials(payload: Any, path: str = "") -> None:
    """Walk the raw JSON and refuse if anything looks like a key."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            here = f"{path}.{k}" if path else k
            if _KEYISH_FIELD.search(str(k)):
                raise HTTPException(
                    422,
                    f"Refused: field '{here}' looks like a credential. "
                    f"This server never accepts keys -- they belong in your browser.",
                )
            # A key can arrive in KEY position too: {"<the credential>": {...}}
            # is what you get from a client that maps results by credential.
            # Only the field-NAME pattern was applied here, so it stored fine.
            if _KEYISH_VALUE.search(str(k)) or _looks_random(str(k)):
                raise HTTPException(
                    422,
                    f"Refused: the field name at '{here}' is itself shaped like a "
                    f"credential. Keys must never leave the browser.",
                )
            assert_no_credentials(v, here)
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            assert_no_credentials(item, f"{path}[{i}]")
    elif isinstance(payload, str):
        if _KEYISH_VALUE.search(payload) or _looks_random(payload):
            raise HTTPException(
                422,
                f"Refused: value at '{path or 'body'}' matches an API-key pattern. "
                f"Keys must never leave the browser.",
            )


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

app = FastAPI(title="AI Crash Test - backend", version="0.1.0")

# ---------------------------------------------------------------------------
# CORS is a SECURITY control here, not a convenience one, because /score
# executes code. With allow_origins=["*"] any page you happen to visit can POST
# arbitrary Python to localhost:8000/score and it runs on your machine. That is
# a dev-machine problem, not just a production one, so the default is closed.
#
# Set NTAI_ORIGINS to a comma-separated list to allow your frontend, e.g.
#   NTAI_ORIGINS="http://localhost:5173" uvicorn server:app --port 8000
# Opening index.html from file:// sends Origin: null; use the static server.
# ---------------------------------------------------------------------------
_ORIGINS = [o.strip() for o in os.environ.get("NTAI_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["content-type", "x-ntai-token"],
)

# A shared token gates the endpoint that executes code. Generated per boot and
# printed once if you don't set one, so the default posture is closed rather
# than open. Set NTAI_TOKEN to keep it stable across restarts.
_TOKEN = os.environ.get("NTAI_TOKEN") or uuid.uuid4().hex
if not os.environ.get("NTAI_TOKEN"):
    print(f"\n[never-touch] /score token for this boot: {_TOKEN}\n"
          f"[never-touch] allowed origins: {_ORIGINS}\n", file=sys.stderr)


async def no_credentials(request: Request) -> None:
    """Run the never-touch guard over the RAW body, before any route sees it.

    assert_no_credentials was wired into /runs alone, but /sweep/expand takes the
    same nested config -- roles, system prompts, substrate base_urls -- ran no
    guard, and ECHOED every config back in its response. The README said the
    guard walks "every incoming payload"; it walked one endpoint's. As a
    dependency it runs before request validation, so it holds for anything that
    accepts a body, including routes added later.
    """
    try:
        raw = await request.json()
    except Exception:
        return          # not JSON -- the route's own validation will reject it
    assert_no_credentials(raw)


def require_token(request: Request) -> None:
    """Gate the code-executing endpoint. Constant-time compare."""
    import hmac
    sent = request.headers.get("x-ntai-token", "")
    if not hmac.compare_digest(sent, _TOKEN):
        raise HTTPException(401, "missing or bad x-ntai-token; /score executes code and is gated")

# In-memory store; swap for SQLite/Postgres (and per-user isolation) in production.
_RUNS: dict[str, dict] = {}


@app.post("/sweep/expand", dependencies=[Depends(no_credentials)])
def expand_sweep(sweep: ConfigSweep) -> dict:
    """Turn a sweep into concrete configs. Pure schema work -- no keys, no calls."""
    configs = sweep.expand()
    out = []
    for c in configs:
        d = c.model_dump()
        d["resolved_graph"] = c.as_graph()      # chain or graph, one shape
        d["order"] = c.order()                  # refuses cycles / dangling edges
        d["terminals"] = c.terminals()          # where the answer comes from
        d["shape"] = _shape_label(c)
        out.append(d)
    return {"count": len(out), "configs": out}


@app.post("/runs")
async def ingest_run(request: Request) -> dict:
    """Store one sanitized run. The raw body is guarded before it is trusted."""
    raw = await request.json()
    assert_no_credentials(raw)                    # <-- never-touch boundary check
    record = RunRecord.model_validate(raw)        # then validate the shape
    run_id = uuid.uuid4().hex[:12]
    _RUNS[run_id] = {"id": run_id, "ts": time.time(), **record.model_dump()}
    return {"stored": run_id}


@app.get("/runs")
def list_runs() -> dict:
    runs = sorted(_RUNS.values(), key=lambda r: r["ts"], reverse=True)
    return {"count": len(runs), "runs": runs}


@app.get("/compare")
def compare() -> dict:
    """Aggregate stored runs by config_name -- the side-by-side comparison."""
    buckets: dict[str, list[dict]] = {}
    for r in _RUNS.values():
        buckets.setdefault(r["config_name"], []).append(r)
    rows = []
    for name, rs in buckets.items():
        n = len(rs)
        scored = [r["score"] for r in rs if r.get("score") is not None]
        rows.append(
            {
                "config_name": name,
                "n": n,
                "mean_score": round(sum(scored) / len(scored), 3) if scored else None,
                "mean_latency_ms": round(sum(r["total_latency_ms"] for r in rs) / n),
                "mean_cost_usd": round(sum(r["total_cost_usd"] for r in rs) / n, 6),
                "success_rate": round(sum(1 for r in rs if r["ok"]) / n, 3),
            }
        )
    rows.sort(key=lambda x: (-(x["mean_score"] if x["mean_score"] is not None else -1),
                             x["mean_cost_usd"]))
    return {"configs": rows}


@app.delete("/runs")
def clear_runs() -> dict:
    n = len(_RUNS)
    _RUNS.clear()
    return {"cleared": n}


# ----------------------------------------------------------------------------
# Scoring -- turns the comparison from "cheaper / faster" into "actually correct".
# This is the differentiator: it runs the harness's generated code against real
# tests. It scores OUTPUT TEXT and never sees or needs an API key.
#
# SECURITY: /score executes model-generated code in a subprocess with only a
# timeout. That is NOT a real sandbox. Before production, isolate it
# (container / gVisor / nsjail, no network, non-root, read-only FS).
# ----------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Text predicates come from gradecore — the same grading engine model-drift and
# the crash test use — so "one engine" is a shared import rather than three
# codebases that agree by coincidence. Code EXECUTION stays here on purpose:
# gradecore is pure (no I/O, no subprocess) and running a candidate is I/O.
# ---------------------------------------------------------------------------
try:
    from gradecore import (GradeInput, contains, exact, number, one_of, regex,
                           valid_json)
    _GRADECORE = True
except ImportError:                                # optional; falls back below
    _GRADECORE = False


class Check(BaseModel):
    # text predicates (gradecore): contains | regex | exact | one_of | number | valid_json
    # execution (local):           python
    kind: str
    pattern: str = ""              # contains / regex / exact
    values: list[str] = []         # one_of
    value: Optional[float] = None  # number
    keys: list[str] = []           # valid_json required keys
    test: str = ""                 # python: asserts appended after candidate code
    weight: float = 1.0
    label: str = ""


def _gradecore_verdict(chk: "Check", output: str):
    """Build the gradecore grader for a text check and run it. None if unsupported."""
    if not _GRADECORE:
        return None
    k = chk.kind
    if k == "contains":   g = contains(chk.pattern)
    elif k == "regex":    g = regex(chk.pattern)
    elif k == "exact":    g = exact(chk.pattern)
    elif k == "one_of":   g = one_of(*chk.values)
    elif k == "valid_json": g = valid_json(*chk.keys)
    elif k == "number":
        if chk.value is None:
            return None
        g = number(chk.value)
    else:
        return None
    return g(GradeInput(text=output))


class ScoreRequest(BaseModel):
    output: str                    # the harness's final output (never a key)
    checks: list[Check]
    language: str = "python"


def _extract_code(text: str) -> str:
    """Pull a fenced code block if present, else use the whole text."""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return m.group(1) if m else text


# Where generated code runs. "subprocess" is the fallback and is NOT a sandbox;
# "docker" is the default when Docker is available. Override with NTAI_SANDBOX.
_SANDBOX = os.environ.get("NTAI_SANDBOX", "auto")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


_USE_DOCKER = (_SANDBOX == "docker") or (_SANDBOX == "auto" and _docker_available())
if not _USE_DOCKER:
    print("[never-touch] WARNING: /score will run generated code in a bare subprocess.\n"
          "[never-touch] That is not isolation. Install Docker, or set NTAI_SANDBOX=docker,\n"
          "[never-touch] before pointing this at output you did not write.", file=sys.stderr)


def _run_python(candidate: str, test: str, timeout: int = 8) -> tuple[bool, str]:
    """Execute candidate code + asserts. Isolated in Docker when available."""
    src = candidate + "\n\n" + test + "\n"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cand.py")
        with open(path, "w") as fh:
            fh.write(src)

        if _USE_DOCKER:
            # No network, read-only root, non-root user, dropped capabilities,
            # capped memory/PIDs, and the code mounted read-only. A hostile
            # generation gets a dead end instead of your laptop.
            cmd = [
                "docker", "run", "--rm",
                "--network", "none",
                "--read-only",
                "--user", "65534:65534",           # nobody
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--pids-limit", "64",
                "--memory", "256m", "--memory-swap", "256m",
                "--cpus", "1.0",
                "-v", f"{path}:/cand.py:ro",
                "--workdir", "/tmp",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "python:3.12-alpine", "python", "/cand.py",
            ]
        else:
            cmd = [sys.executable, path]

        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout + (12 if _USE_DOCKER else 0), cwd=d)
            return p.returncode == 0, ("" if p.returncode == 0 else p.stderr[-400:])
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
        except FileNotFoundError as e:
            return False, f"sandbox unavailable: {e}"


@app.post("/score")
def score(req: ScoreRequest, request: Request) -> dict:
    require_token(request)          # <-- this endpoint runs code; gate it
    code = _extract_code(req.output)
    results, got, total = [], 0.0, 0.0
    for chk in req.checks:
        total += chk.weight
        engine = "gradecore"
        if chk.kind == "python":
            ok, detail = _run_python(code, chk.test)
            engine = "local-exec"                  # execution is I/O; not gradecore's job
        else:
            v = _gradecore_verdict(chk, req.output)
            if v is not None:
                ok, detail = v.passed, v.detail
            elif chk.kind in ("contains", "regex"):
                # gradecore absent — same semantics, so a missing optional dep
                # degrades rather than fails, but say which engine ran.
                engine = "fallback"
                ok = (chk.pattern in req.output) if chk.kind == "contains" \
                     else re.search(chk.pattern, req.output) is not None
                detail = ""
            else:
                ok, detail = False, f"unknown or unsupported check kind '{chk.kind}'"
                engine = "none"
        if ok:
            got += chk.weight
        results.append({"label": chk.label or chk.kind, "ok": ok,
                        "detail": detail, "engine": engine})
    return {
        "score": round(got / total, 3) if total else 0.0,
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
        "results": results,
        "gradecore": _GRADECORE,     # is the shared engine actually in play?
    }


@app.get("/suite")
def get_suite() -> dict:
    """Serve the curated coding suite -- the asset that makes this an evaluator,
    not just a runner. Swap in your own tasks (or serve several suites) here."""
    path = os.path.join(os.path.dirname(__file__), "coding_suite.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise HTTPException(404, "coding_suite.json not found next to server.py")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "runs_stored": len(_RUNS)}
