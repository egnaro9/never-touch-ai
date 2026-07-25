# AI Crash Test — skeleton

A hosted BYOK tool for testing and comparing AI harness configurations: different
models bound to different roles, with different scaffolding, run against the same
task and scored side by side. The user brings their own API key, and **the key
never touches the server** — it stays in the browser tab, in memory only.

This is a working skeleton, not a product. It runs end to end and is meant to be
read and extended.

---

## How "never touch" is actually enforced

The name is a promise, so every layer is built to keep it literally true:

1. **Key in memory only.** In `index.html` the key is a single JavaScript
   variable (`API_KEY`). It is never written to `localStorage`/`sessionStorage`,
   never posted to the backend, and is dropped on refresh or `beforeunload`.
2. **The browser makes the provider calls.** `callAnthropic()` calls
   `https://api.anthropic.com/v1/messages` directly from the browser, using the
   opt-in header `anthropic-dangerous-direct-browser-access: true`. No proxy sits
   in the middle, so the key never transits your infrastructure. ("Dangerous"
   refers to embedding *your own* key in shipped code — a non-issue in BYOK,
   where it's the user's key in the user's browser.)
3. **The backend only stores sanitized traces.** The browser posts run records
   containing configs, models, latency, token counts, and cost — never the key
   and never the raw calls.
4. **The boundary rejects credentials.** `server.py`'s `assert_no_credentials()`
   walks every incoming payload and returns `422` if any field name *or value*
   looks like a key. So even a buggy client can't leak a key into storage.
5. **No frontend supply chain.** The client is dependency-free vanilla JS — no
   npm packages that could be compromised to exfiltrate a key from memory.
6. **The code-executing endpoint is closed by default.** `/score` runs
   model-generated code, so it requires an `x-ntai-token` header (printed on
   boot, or set `NTAI_TOKEN`) and CORS defaults to `http://localhost:5173`
   rather than `*`. Without this, any page you visited could POST code to
   `localhost:8000/score` and have it run — a dev-machine risk, not just a
   production one.

Since the whole client is readable source, the "we never store your key" claim is
verifiable rather than asked-on-faith. Open-sourcing it is the point.

---

## Run it

**Backend** (Python 3.10+):

```bash
pip install "fastapi>=0.110" "uvicorn[standard]"
NTAI_TOKEN=pick-a-secret NTAI_ORIGINS=http://localhost:5173 \
  uvicorn server:app --reload --port 8000
```

`/score` executes generated code, so it is token-gated and CORS is closed by
default. Paste the same token into the field beside the backend URL in the UI.
Install Docker and scoring runs isolated automatically (no network, read-only,
non-root, memory/PID capped); without it you get a loud warning and a bare
subprocess, which is **not** isolation.

**Frontend:** open `index.html` directly, or serve it statically:

```bash
python -m http.server 5173   # then visit http://localhost:5173
```

Paste your Anthropic key into the bar at the top, pick a **task source** — a single
task or the **curated coding suite** — then **Run sweep**. The default config is a
manager→worker pipeline with a 2-axis sweep, so the first run already gives you a
4-way comparison. Scoring and the suite need the backend running; the sweep itself
also works with the backend offline (results compute locally, minus scores).

---

## The config model

A harness config is **data**, which is what makes combinations enumerable:

```jsonc
{
  "base": {
    "name": "manager→worker",
    "roles": {
      "manager": { "model": "claude-opus-4-8", "system": "…plan…", "max_tokens": 400 },
      "worker":  { "model": "claude-sonnet-5", "system": "…implement…", "max_tokens": 900 }
    },
    "pipeline": ["manager", "worker"]      // topology: each role's output feeds the next
  },
  "axes": [                                 // vary fields → cartesian product of configs
    { "role": "manager", "field": "model", "values": ["claude-opus-4-8", "claude-sonnet-5"] },
    { "role": "worker",  "field": "model", "values": ["claude-sonnet-5", "claude-haiku-4-5-20251001"] }
  ]
}
```

### Three kinds of axis

A harness has three genuinely different variables, and the sweep can vary each:

```jsonc
"substrates": {                                   // named serving paths
  "anthropic-direct": { "shape": "anthropic", "base_url": "" },
  "local-vllm":       { "shape": "openai", "base_url": "http://localhost:8001/v1" }
},
"axes": [
  { "kind": "role",      "role": "manager", "field": "model",
    "values": ["claude-opus-4-8", "claude-sonnet-5"] },   // who does the job
  { "kind": "topology",  "values": [["worker"], ["manager","worker"]] },
                                                          // is the role needed at all
  { "kind": "substrate", "role": "*", "values": ["anthropic-direct", "local-vllm"] }
                                                          // same model id, different serving path
]
```

**Topology** is a **graph**, not just a chain, so the harness can branch:

```jsonc
"graph": {
  "planner": [],                          // no inputs -> gets the task
  "draft_a": ["planner"],                 // two drafters, in parallel
  "draft_b": ["planner"],                 //   (draft_b runs on another vendor)
  "judge":   ["draft_a", "draft_b"]       // fan back in and pick
}
```

A node with no inputs receives the task; otherwise it receives its parents'
outputs, each tagged `<<parent>>` so a judge can tell whose draft is whose. The
answer comes from the **terminal** nodes — whatever nothing else consumes. Cycles
and edges into missing roles are refused by name before any call is paid for, and
`pipeline: [a, b]` still works as sugar for the chain `{a:[], b:[a]}`.

Because each role carries its own `substrate`, one branch can be Claude and
another GPT — a **cross-vendor panel judged by a third model** is just a shape:

    solo:  draft_a                            1 call
    chain: planner→draft_a                    2 calls
    panel: planner→[draft_a|draft_b]→judge    4 calls, 2 vendors

Sweeping those three answers the question a model-only comparison cannot ask: is
the panel worth 4× the calls, or does one drafter match it?

**Substrate** is the one most tools ignore. A model id is not a system: the same
id can be served from different endpoints, hardware, quantizations and point
releases, and the response comes back 200, well-formed and gradeable either way.
Holding the id fixed while varying the serving path is the only way to see it.
Each step's trace records which substrate served it, so the comparison is
auditable rather than assumed. (That the serving path is an unmeasured third
variable — alongside the model and the grader — is ANP2 Network's observation.)

`axes` expands to every combination. Each config runs **N times**
(*runs per config*) so you compare aggregates, not noisy single shots. A
client-side **spend cap** halts the sweep before it can overrun.

Pricing lives in the `PRICING` map (USD per 1M tokens) — verify current numbers at
<https://claude.com/pricing>; they change, and Sonnet 5 has intro pricing through
2026-08-31.

---

## The coding suite

The real differentiator isn't the plumbing — it's the tasks you score against.
Pick **Curated coding suite** as the task source and the tool runs every config
across the whole suite, then shows a per-config **leaderboard** (suite pass-rate,
total cost) plus a **task × config matrix** so you can see exactly which tasks each
config passes.

`coding_suite.json` ships with **20 validated tasks** across categories — code
generation, bug-fixing, refactoring, class design (incl. an LRU cache and a
retry decorator), parsing, and classic algorithms — each with execution checks.
Every task is verified against a reference solution *before* it enters the file,
so a passing score means the generated code actually runs correctly, not that it
looked plausible.

Task format:

```json
{
  "id": "bsearch",
  "category": "algorithm",
  "prompt": "Write `bsearch(arr, target) -> int` ... Return only the Python code.",
  "checks": [
    { "kind": "python", "label": "bsearch",
      "test": "assert bsearch([1,3,5], 2) == -1\nassert bsearch([5], 5) == 0\nprint('ok')" }
  ]
}
```

Two rules keep execution scoring reliable: **pin the interface** in the prompt
(exact function/class name and signature) so the `test` can call it, and make the
asserts cover edge cases so a superficially-right answer fails. To grow the suite,
add tasks to `coding_suite.json` (served at `GET /suite`) — that curated set is the
asset worth investing in.

---

## What's deliberately left for you

This skeleton owns the hard, load-bearing parts (key custody, config sweeps,
sanitized telemetry). The rest is where the product lives:

- **Scoring + curated suite (included — the differentiator).** `/score` runs the
  harness's generated code against `python` asserts (plus `contains`/`regex`), and
  the leaderboard **ranks by correctness first, then cost** — so a pricier config
  that actually passes beats a cheap one that doesn't. A run that *errored* scores
  0 rather than being dropped, because excluding failures let a config that died
  9 times out of 10 show 100% off its one good run. `coding-starter-v2`
  (20 validated tasks) ships in `coding_suite.json`. Grow it toward SWE-bench /
  Terminal-Bench scale. Scoring runs on output text only — never a key.
- **Grading is shared, not copied.** Text predicates (`contains`, `regex`, `exact`,
  `one_of`, `number`, `valid_json`) are imported from
  [gradecore](https://github.com/egnaro9/gradecore) — the same engine model-drift
  and the crash test grade with, so "one engine" is an import rather than three
  codebases that agree by coincidence. Every result names the engine that produced
  it (`gradecore` / `local-exec` / `fallback`). Code **execution** deliberately
  stays here: gradecore is pure by design, and running a candidate is I/O.
- **More providers.** Add adapters for OpenAI/Google (both support CORS). A
  provider without CORS can't be called browser-direct without a proxy that would
  touch the key — so your supported list is effectively "the CORS-friendly ones."
- **DAG topology.** `pipeline` is a linear list; generalize to a graph for
  branching/critic loops without changing the schema's shape.
- **Persistence + tenancy.** Swap the in-memory store for a real DB with strict
  per-user isolation so no one sees another's runs.
- **Interactive, not batch.** Because the browser holds the key and does the work,
  a closed tab stops the run — there's no server worker to hand off to. That's
  inherent to the model; design for a tool people watch.

### Production security checklist

- Tighten `allow_origins` from `*` to your exact frontend origin.
- Ship a strict Content-Security-Policy (the key lives in the DOM's JS context —
  an XSS bug is now the main way it could leak).
- Keep the key in memory only; never add a "remember my key" checkbox.
- Add auth and per-user rate limiting on the backend endpoints.
- **Sandbox the scorer.** Done for the common case: with Docker present, `/score`
  runs each candidate in `python:3.12-alpine` with `--network none`, `--read-only`,
  non-root, `--cap-drop ALL`, and memory/PID caps. Without Docker it falls back to a
  bare subprocess and says so loudly. For untrusted multi-tenant use, go further
  (gVisor / nsjail / a dedicated runner host).
- **Two sweep implementations, on purpose.** The browser expands the sweep (it must
  work offline) and so does `server.py`. Rather than let them rot apart, the client
  calls `/sweep/expand` before each run and refuses to start if the two disagree —
  the same differential-oracle trick this tool sells, pointed at itself.
