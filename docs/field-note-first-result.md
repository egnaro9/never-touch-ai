# Field note — the first real sweep, and why it refused to call a winner

> A write-up of the Harness Builder's first run against real models, engineer to
> engineer. Longer and more technical than the social post: the graph execution
> model, why the comparison is a paired sign test rather than two averages, and
> the bugs the run itself surfaced. The scores, costs, latencies and win/loss
> counts are recomputed from
> [`results/sweep_2026-07-25.json`](../results/sweep_2026-07-25.json), which is
> committed; the test counts come from the commands at the end.

**Run it yourself:** https://egnaro9.github.io/harness-builder/sweep.html — draw a
harness, sweep it, and it executes the whole matrix on mock substrates for free,
with no key at all.
**Source:** [`egnaro9/harness-builder`](https://github.com/egnaro9/harness-builder)
· the grader engine: [`egnaro9/gradecore`](https://github.com/egnaro9/gradecore)

---

## The result

Twenty coding tasks, three harness shapes, one run each, $0.99, zero errors
across sixty runs.

| harness | calls/task | score | cost | mean latency |
|---|---|---|---|---|
| one drafter | 1 | **95%** | $0.031 | 2.2s |
| planner → drafter | 2 | 90% | $0.264 | 9.1s |
| planner → 2 drafters → judge | 4 | **80%** | $0.692 | 18.3s |

The four-call panel beat the single drafter on **zero** of twenty tasks, lost
three (`rle_encode`, `min_stack`, `two_sum`), and tied seventeen. Models were
Opus 4.8 planning and judging, Sonnet 5 and Haiku 4.5 drafting.

I built this expecting the opposite. That is most of why I trust the number.

## Where it sits in the field (so I don't oversell it)

Harness and prompt-comparison tooling is not a new category — promptfoo,
LangSmith, Braintrust and others all do model and prompt comparison, several
with far more surface area than this. Multi-agent frameworks all ship their own
evaluation stories.

The narrow thing this does is an **intersection**: browser-BYOK, **plus**
deterministic no-LLM-judge grading, **plus** a comparison that reports when it
cannot decide. That last part is the one I have not seen elsewhere, and it is
the reason the headline above is not the whole finding.

## Why the comparison is a sign test, not two averages

95% against 80% looks decisive. It is not, and the tool says so:

> Only 3 tasks separated them. Even a clean sweep of 3 could not clear p<0.05, so
> **this suite cannot decide** between them — that is a limit of the suite, not a
> finding about the harnesses.

Seventeen of twenty tasks were ties. Ties carry no information about direction,
so they are excluded, which leaves three informative tasks. A two-sided exact
sign test on three discordant pairs gives p = 0.25 at best — a clean sweep still
cannot reach significance.

Both shapes run the **same** tasks, so the comparison is paired: how many tasks
did A win, not what were the two means. At n=20 that is far more sensitive than
comparing averages, and it is the honest form of the question. The test assumes
almost nothing — no normality, no equal variance, no interval scale — only that
under the null each disagreement falls either way with equal probability.

So the reading splits into three claims a leaderboard would collapse into one:

1. There is **no evidence** the panel helps on this suite.
2. It costs 22.1× more and takes 8.4× longer. Measured, not inferred.
3. Whether it is genuinely *worse* needs more tasks than twenty.

The binding constraint is not step size — the 15-point gap is three times the
5-point resolution. It is the number of tasks the two shapes actually *disagreed*
on: three. Everything else was a tie, and ties are exactly what a paired test
throws away. Twenty tasks is simply too few to produce enough disagreements for
any test to work with. The fix is more tasks, not better statistics — which is
why the tool takes your own suite and tells you, as you paste it, how many points
each task is worth.

## The execution model

A harness config is data: roles, models, prompts, and a topology **graph**
mapping each role to the roles feeding it.

```json
{"planner": [], "draft_a": ["planner"], "draft_b": ["planner"],
 "judge": ["draft_a", "draft_b"]}
```

A node with no parents receives the task. A node with one parent receives that
parent's output. A node with several receives each parent's output tagged
`<<parent>>`, so a judge can tell whose draft is whose. Terminals produce the
answer. Cycles and dangling edges are refused by name before anything is paid
for.

The fan-in is the part a linear pipeline cannot express. A node with several
parents has its prompt assembled from every parent's output, each wrapped in its
own tag — `sweep.html:1372` is the whole of it:

```js
deps.map((d) => `<<${d}>>\n${outputs[d]}`).join("\n\n")
```

The tags come from the declared graph rather than from sniffing the text, so both
drafts reach the judge by construction. I am not quoting token overheads here:
the committed result file records scores, cost and latency only, and a number a
reader cannot recompute does not belong in a note whose whole point is that you
can check it.

## The grade path has no model in it

Scores come from fixed predicates, not from a model. `gradecore` supplies the
text and scalar checks and is pure by design; running a candidate is I/O, so
execution stays in this repo behind a separate `local-exec` engine
(`server.py:649`). No model grades anything.

That makes the *grading* deterministic — the same output always scores the same.
It does not make the pipeline deterministic: no temperature or seed is set
anywhere, and this sweep used one run per config, so a 5-point move between two
runs of the same shape is well within sampling noise. That is the argument for
more tasks and more runs per config, and it is why the tool refuses to call a
winner off a suite this small.

There is an assistant in the page. It may **read** scores and explain them; it
may never produce one. Its config proposals go through the same validation a
human gesture does — `parseConfig → expandSweep → orderOf` — so it cannot write
a config the runner would refuse, and it cannot approve its own output.

## Your key never reaches the server

The browser calls the provider directly. The backend receives sanitized run
traces and refuses any payload that looks key-shaped at its boundary. The page
shows you the exact bytes it posts and tells you to check your own Network tab
rather than believe the panel.

One vendor fact worth writing down, because it cost me an hour: **`api.openai.com`
answers the CORS preflight with the right headers and then omits
`access-control-allow-origin` on the actual response.** A browser-direct call is
discarded no matter how valid the key is. Anthropic opts in deliberately — that
is what `anthropic-dangerous-direct-browser-access` is for. Testing only the
preflight with `curl -X OPTIONS` shows success and is misleading.

## What the run itself found

The first live attempt returned 401 on every call. Two things came out of that:

- The request shape was verified correct before anything was blamed — right URL,
  `x-api-key`, `anthropic-version`, opt-in header, well-formed body. It was the
  key, and it was expired.
- Diagnosing it required pasting a key into a terminal, which is a gap in the
  tool. Every provider row now has a **test** button: one request, one token, and
  it reports whether the provider rejected the key or the browser blocked the
  call — two failures that look identical in a run log and have completely
  different fixes.

And a bug the 401 exposed: the default key field re-rendered its own input on
every keystroke, so a **typed** key lost focus after the first character and was
silently truncated. A truncated key fails as "invalid", which sends you to check
your account instead of the page. Pasting fires one event and survived, which is
why only a live run surfaced it.

## Reproduce it

```bash
git clone https://github.com/egnaro9/harness-builder && cd harness-builder
python3 -m pip install -r requirements.txt
python3 -m pytest tests -q           # 130 passed, 6 xfailed
node --test 'tests/*.test.js'        # 43 passed

# the numbers in this note, recomputed from the committed result
python3 - <<'EOF'
import json
d = json.load(open("results/sweep_2026-07-25.json"))
for c in d["configs"]:
    print(f'{c["calls_per_task"]} call(s): {c["mean_score"]*100:5.1f}%  '
          f'${c["total_cost_usd"]:.4f}  {c["mean_latency_ms"]}ms')
print("total spent: $", d["total_spent_usd"])
EOF
```

To run your own sweep: open `sweep.html`, click the **Dry run** shape, and it
executes the whole matrix on mock substrates for nothing. Add a key when you want
real models.

---

*Built by [Erik Hill](https://egnaro9.github.io). The tool told me the
architecture I assumed was better is not better — and then refused to let me
say it was worse. Both halves are the point.*
