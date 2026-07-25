"""/sweep/expand -- the cartesian product, config naming, and the three axis kinds.

Expansion is the one place where a typo becomes N typos. Every config this
endpoint emits is a thing the browser will actually run with the user's key and
the user's money, so a config that is merely *shaped* like JSON is not good
enough: it has to be runnable. That is why the endpoint resolves the graph and
topologically sorts it before answering -- a topology that cannot be ordered is
refused here, cheaply, instead of being handed to the browser which used to
accept it and then stop halfway with nothing on screen.

The other half of the job is the NAME. Names are the join key between a config
and its stored runs (`/runs` and `/compare` bucket by `config_name`), so a name
that drifts or collides silently merges two experiments into one row. These
tests pin the name format as tightly as they pin the topology rules.
"""
import pytest

from conftest import expand


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _detail(payload) -> str:
    """Flatten a 422 body to one string.

    Refusals arrive two different ways -- HTTPException raised inside expand()
    gives `detail` as a string, Pydantic schema failures give a list of error
    dicts -- and a test that only understands one of them silently stops
    checking the message when the refusal moves between the two layers.
    """
    return str(payload.get("detail", payload)) if isinstance(payload, dict) else str(payload)


def _with_substrates(base: dict) -> dict:
    """The same base plus two named serving paths. Substrate axes need these
    declared up front; the axis selects among them, it cannot invent one."""
    return dict(
        base,
        substrates={
            "direct": {"name": "direct", "shape": "anthropic"},
            "bedrock": {"name": "bedrock", "shape": "anthropic",
                        "base_url": "https://bedrock.example"},
        },
    )


def _fan_base() -> dict:
    """Four roles, no topology yet -- the shape gets supplied by a topology axis.
    Needed because a two-role chain cannot express fan-out or fan-in at all."""
    return {
        "name": "fan",
        "roles": {
            "planner": {"model": "m"},
            "draft_a": {"model": "m"},
            "draft_b": {"model": "m"},
            "judge": {"model": "m"},
        },
        "pipeline": ["planner", "draft_a"],   # placeholder; every test overrides it
        "graph": {},
    }


# ---------------------------------------------------------------------------
# The product itself
# ---------------------------------------------------------------------------

def test_zero_axes_returns_exactly_the_base_config(client, base_cfg):
    """A sweep with no axes must be a no-op passthrough.

    The UI calls /sweep/expand unconditionally, including before the user has
    added a single axis, to render the "you are about to run this" preview. If
    zero axes returned zero configs the Run button would do nothing; if it
    returned a *renamed* config the preview would disagree with what later shows
    up in /compare.
    """
    status, payload = expand(client, base_cfg, [])
    assert status == 200
    assert payload["count"] == 1

    cfg = payload["configs"][0]
    assert cfg["name"] == "t"                      # no bracket suffix, no relabelling
    assert cfg["roles"]["planner"]["model"] == "claude-opus-4-8"
    assert cfg["roles"]["worker"]["model"] == "claude-sonnet-5"
    assert cfg["graph"] == {"planner": [], "worker": ["planner"]}
    assert cfg["runs_per_config"] == 3


def test_zero_axes_preserves_a_name_that_already_contains_brackets(client, base_cfg):
    """The base name is copied verbatim, not reformatted.

    Labels are appended as " [...]" so a base name that already has brackets is
    the case where a naive "strip the suffix" round-trip would eat part of the
    user's own name and break the join back to their stored runs.
    """
    base_cfg["name"] = "baseline v2 [prod]"
    status, payload = expand(client, base_cfg, [])
    assert status == 200
    assert payload["configs"][0]["name"] == "baseline v2 [prod]"


def test_one_axis_yields_one_config_per_value(client, base_cfg):
    """N values must produce exactly N configs, in the order given.

    The browser pairs its own expansion against this one positionally. Dropping
    or reordering a value here means every downstream result is attributed to
    the wrong model.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model",
         "values": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4"]},
    ])
    assert status == 200
    assert payload["count"] == 3
    assert [c["roles"]["planner"]["model"] for c in payload["configs"]] == [
        "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4",
    ]


def test_two_axes_are_a_full_cartesian_product_with_the_last_axis_fastest(client, base_cfg):
    """2 x 3 must be 6 configs, ordered with the LAST axis varying fastest.

    Enumeration order is part of the contract, not an implementation detail: it
    is how a sweep resumed after a browser reload lines back up with the results
    already stored. A silent switch to first-axis-fastest re-labels every row.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model", "values": ["a", "b"]},
        {"kind": "role", "role": "worker", "field": "max_tokens", "values": [256, 512, 1024]},
    ])
    assert status == 200
    assert payload["count"] == 6
    assert [c["name"] for c in payload["configs"]] == [
        "t [planner.model=a, worker.max_tokens=256]",
        "t [planner.model=a, worker.max_tokens=512]",
        "t [planner.model=a, worker.max_tokens=1024]",
        "t [planner.model=b, worker.max_tokens=256]",
        "t [planner.model=b, worker.max_tokens=512]",
        "t [planner.model=b, worker.max_tokens=1024]",
    ]


def test_all_three_axis_kinds_multiply_together(client, base_cfg):
    """topology x substrate x role must compose into 8 configs.

    The three kinds mutate different parts of the config (graph, roles[].substrate,
    roles[].<field>). If any pair fought over the same copy -- e.g. the topology
    branch resetting roles -- the count would still be 8 while the contents were
    wrong, so this checks a spot value, not just the arithmetic.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "topology", "values": [["worker"], ["planner", "worker"]]},
        {"kind": "substrate", "role": "*", "values": ["direct", "bedrock"]},
        {"kind": "role", "role": "worker", "field": "model", "values": ["a", "b"]},
    ])
    assert status == 200
    assert payload["count"] == 8

    last = payload["configs"][-1]
    assert last["name"] == "t [planner→worker, on=bedrock, worker.model=b]"
    assert last["resolved_graph"] == {"planner": [], "worker": ["planner"]}
    assert last["roles"]["worker"]["substrate"] == "bedrock"
    assert last["roles"]["worker"]["model"] == "b"


def test_every_config_carries_resolved_graph_order_terminals_and_shape(client, base_cfg):
    """The four derived fields must be present on EVERY config, not just the first.

    The browser reads `order` to decide what to call and in what sequence, and
    `terminals` to decide which output to grade. A config missing them is not a
    cosmetic gap -- the runner has nothing to execute and nothing to score.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model", "values": ["a", "b", "c"]},
    ])
    assert status == 200
    for cfg in payload["configs"]:
        assert set(cfg) >= {"resolved_graph", "order", "terminals", "shape"}
        assert cfg["resolved_graph"] == {"planner": [], "worker": ["planner"]}
        assert cfg["order"] == ["planner", "worker"]     # deps before dependents
        assert cfg["terminals"] == ["worker"]            # the answer comes from here
        assert cfg["shape"] == "planner→worker"


def test_count_agrees_with_the_configs_array(client, base_cfg):
    """`count` is what the UI shows before spending money -- it must not lie.

    "This will run 6 configs x 3 repeats" is the last number a user sees before
    committing their own API budget. A count computed separately from the list
    is exactly how that estimate drifts from reality.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "*", "values": ["direct", "bedrock"]},
        {"kind": "role", "role": "planner", "field": "model", "values": ["a", "b", "c"]},
    ])
    assert status == 200
    assert payload["count"] == len(payload["configs"]) == 6


# ---------------------------------------------------------------------------
# Naming -- the join key between a config and its runs
# ---------------------------------------------------------------------------

def test_name_records_every_axis_in_axis_order(client, base_cfg):
    """Each axis contributes one label, comma-joined, in the order declared.

    A name is the only record of WHY a row won. Dropping an axis from the label
    produces two rows with identical names that /compare then averages together,
    which turns a real difference into no difference.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "topology", "values": [["planner", "worker"]]},
        {"kind": "substrate", "role": "*", "values": ["bedrock"]},
        {"kind": "role", "role": "worker", "field": "model", "values": ["z"]},
    ])
    assert status == 200
    assert payload["configs"][0]["name"] == "t [planner→worker, on=bedrock, worker.model=z]"


def test_role_axis_label_is_role_dot_field_equals_value(client, base_cfg):
    """Role labels must name the role AND the field, not just the value.

    Two axes sweeping "model" on two different roles produce the same values;
    without the `role.field=` prefix the leaderboard cannot tell "the planner
    got smarter" from "the worker got smarter".
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "worker", "field": "max_tokens", "values": [4096]},
    ])
    assert status == 200
    assert payload["configs"][0]["name"] == "t [worker.max_tokens=4096]"


def test_long_axis_values_are_truncated_in_the_name(client, base_cfg):
    """Values over 24 chars are elided to 21 + "..." so names stay readable.

    System prompts are legitimate axis values and they are paragraphs. Without
    the cap a single config name is a wall of text that breaks every leaderboard
    row and every log line it appears in.
    """
    long_model = "a-really-long-model-identifier-that-exceeds-24-chars"
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model", "values": [long_model]},
    ])
    assert status == 200
    # truncation is cosmetic -- the config itself must still carry the full value
    assert payload["configs"][0]["name"] == "t [planner.model=a-really-long-model-i...]"
    assert payload["configs"][0]["roles"]["planner"]["model"] == long_model


def test_value_of_exactly_24_chars_is_not_truncated(client, base_cfg):
    """The boundary is <= 24 kept, 25+ elided. Off-by-one here is a silent rename.

    A model id that sits exactly on the limit must produce a stable name across
    releases, otherwise the same config joins to a different bucket in /compare
    than the runs already stored under the old spelling.
    """
    exactly_24 = "x" * 24
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model", "values": [exactly_24]},
    ])
    assert status == 200
    assert payload["configs"][0]["name"] == f"t [planner.model={exactly_24}]"
    assert "..." not in payload["configs"][0]["name"]


def test_topology_chain_label_is_the_arrow_chain(client, base_cfg):
    """A chain topology labels itself "a→b", matching `shape`.

    "config 3" tells a reviewer nothing. The whole point of the topology axis is
    answering "do I even need a manager?", and that answer is unreadable unless
    the row says which shape ran.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [["worker"], ["planner", "worker"]]},
    ])
    assert status == 200
    assert [c["name"] for c in payload["configs"]] == ["t [worker]", "t [planner→worker]"]
    assert [c["shape"] for c in payload["configs"]] == ["worker", "planner→worker"]


def test_topology_graph_label_brackets_parallel_branches(client):
    """Fan-out/fan-in renders as "planner→[draft_a|draft_b]→judge".

    A branching shape flattened to a line reads as a chain, which is the one
    thing a topology sweep exists to distinguish. Parallel siblings are sorted
    so the label is stable run to run rather than dict-order dependent.
    """
    status, payload = expand(client, _fan_base(), [
        {"kind": "topology", "values": [{
            "planner": [], "draft_a": ["planner"], "draft_b": ["planner"],
            "judge": ["draft_a", "draft_b"],
        }]},
    ])
    assert status == 200
    cfg = payload["configs"][0]
    assert cfg["shape"] == "planner→[draft_a|draft_b]→judge"
    assert cfg["name"] == "fan [planner→[draft_a|draft_b]→judge]"
    assert cfg["order"] == ["planner", "draft_a", "draft_b", "judge"]
    assert cfg["terminals"] == ["judge"]      # only the judge's output gets graded


def test_substrate_label_distinguishes_wildcard_from_single_role(client, base_cfg):
    """"on=X" means every role moved; "role@X" means only that one did.

    Conflating them makes "is Sonnet-via-Bedrock the same thing as Sonnet-direct?"
    unanswerable, because the row no longer says how much of the harness moved.
    """
    status, all_roles = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "*", "values": ["bedrock"]},
    ])
    assert status == 200
    assert all_roles["configs"][0]["name"] == "t [on=bedrock]"

    status, one_role = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "planner", "values": ["bedrock"]},
    ])
    assert status == 200
    assert one_role["configs"][0]["name"] == "t [planner@bedrock]"


def test_names_are_unique_across_the_whole_product(client, base_cfg):
    """No two configs in one expansion may share a name.

    /compare buckets runs by `config_name`. Two configs with one name is not a
    cosmetic clash -- their results get averaged into a single row and the
    comparison quietly reports the mean of two different systems.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "topology", "values": [["worker"], ["planner", "worker"]]},
        {"kind": "substrate", "role": "*", "values": ["direct", "bedrock"]},
        {"kind": "role", "role": "planner", "field": "model", "values": ["a", "b"]},
    ])
    assert status == 200
    names = [c["name"] for c in payload["configs"]]
    assert len(set(names)) == len(names) == 8


# ---------------------------------------------------------------------------
# kind="role"
# ---------------------------------------------------------------------------

def test_role_axis_touches_only_the_targeted_role_and_field(client, base_cfg):
    """Setting planner.model must leave the worker and every other field alone.

    Each config is deep-copied from the base; if the copy were shallow the axes
    would bleed into each other and every config in the sweep would end up
    carrying the last value written, making the whole comparison a no-op.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model", "values": ["a", "b"]},
    ])
    assert status == 200
    first, second = payload["configs"]
    assert first["roles"]["planner"]["model"] == "a"
    assert second["roles"]["planner"]["model"] == "b"
    # untouched neighbours
    for cfg in payload["configs"]:
        assert cfg["roles"]["planner"]["system"] == "plan"
        assert cfg["roles"]["planner"]["max_tokens"] == 1024
        assert cfg["roles"]["worker"] == {
            "model": "claude-sonnet-5", "system": "do",
            "max_tokens": 1024, "substrate": None,
        }


def test_role_axis_can_sweep_the_system_prompt(client, base_cfg):
    """Scaffolding is a first-class axis, not just the model id.

    "Same model, different system prompt" is the cheapest real experiment this
    tool offers; if `field` only worked for `model` the tool would silently be
    half a product.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "worker", "field": "system",
         "values": ["be terse", "think step by step"]},
    ])
    assert status == 200
    assert [c["roles"]["worker"]["system"] for c in payload["configs"]] == [
        "be terse", "think step by step",
    ]


def test_role_axis_with_unknown_role_is_refused_by_name(client, base_cfg):
    """REGRESSION: a role axis pointing at a role that does not exist must 422.

    Renaming a role in `roles` while an axis still names the old spelling is the
    common way this happens. Expanding anyway produces configs where the axis
    did nothing, so the sweep runs, costs money, and reports that the variable
    made no difference -- the worst possible failure, because it looks like data.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "manager", "field": "model", "values": ["a"]},
    ])
    assert status == 422
    assert "manager" in _detail(payload)


def test_role_axis_kind_defaults_to_role_when_omitted(client, base_cfg):
    """An axis with no `kind` is a role axis, for configs written before kinds existed.

    Saved sweeps are JSON on disk in the user's browser. If the default flipped,
    every sweep saved before topology/substrate axes shipped would stop loading.
    """
    status, payload = expand(client, base_cfg, [
        {"role": "planner", "field": "model", "values": ["a"]},
    ])
    assert status == 200
    assert payload["configs"][0]["roles"]["planner"]["model"] == "a"
    assert payload["configs"][0]["name"] == "t [planner.model=a]"


def test_unknown_axis_kind_is_refused(client, base_cfg):
    """Only role|topology|substrate are accepted; a typo must not become a no-op.

    An unrecognised kind silently falling through to the `role` branch would
    apply the wrong mutation, and the user would be comparing configs that
    differ by nothing.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topolgy", "values": [["worker"]]},        # deliberate typo
    ])
    assert status == 422
    assert "topolgy" in _detail(payload) or "kind" in _detail(payload)


# ---------------------------------------------------------------------------
# kind="topology"
# ---------------------------------------------------------------------------

def test_topology_chain_replaces_the_base_graph(client, base_cfg):
    """A chain value must overwrite `graph`, not merge with it.

    base_cfg is written in graph form. If the chain were merged rather than
    replaced, the "no manager" arm of the sweep would still run the manager and
    the experiment would answer the wrong question.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [["worker"]]},
    ])
    assert status == 200
    cfg = payload["configs"][0]
    assert cfg["graph"] == {}                        # the base graph is cleared
    assert cfg["pipeline"] == ["worker"]
    assert cfg["resolved_graph"] == {"worker": []}   # both forms resolve to one shape
    assert cfg["order"] == ["worker"]
    assert cfg["terminals"] == ["worker"]


def test_topology_may_drop_a_role_from_the_graph_while_keeping_its_definition(client, base_cfg):
    """A dropped role stays in `roles` but leaves the graph. This is intentional.

    "Do I even need a manager?" means running without the manager, then putting
    it back for the next arm of the sweep. Deleting the definition would make
    the two arms non-comparable and break any later axis that names that role.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [["worker"]]},
        {"kind": "role", "role": "planner", "field": "model", "values": ["x"]},
    ])
    assert status == 200
    cfg = payload["configs"][0]
    assert "planner" in cfg["roles"]                 # definition survives
    assert cfg["roles"]["planner"]["model"] == "x"   # and the role axis still applies
    assert "planner" not in cfg["resolved_graph"]    # but nothing calls it
    assert cfg["order"] == ["worker"]


def test_topology_graph_form_expresses_fan_out_and_fan_in(client):
    """The dict form must produce a real DAG, ordered deps-first.

    A chain cannot express two drafters feeding one judge. If the graph form
    collapsed to a line the judge would run before the drafts it is supposed to
    read, and grade an empty input.
    """
    status, payload = expand(client, _fan_base(), [
        {"kind": "topology", "values": [{
            "planner": [], "draft_a": ["planner"], "draft_b": ["planner"],
            "judge": ["draft_a", "draft_b"],
        }]},
    ])
    assert status == 200
    cfg = payload["configs"][0]
    assert cfg["resolved_graph"]["judge"] == ["draft_a", "draft_b"]
    order = cfg["order"]
    assert order.index("planner") < order.index("draft_a") < order.index("judge")
    assert order.index("draft_b") < order.index("judge")
    assert cfg["terminals"] == ["judge"]


def test_topology_parent_naming_a_node_outside_the_graph_is_refused(client, base_cfg):
    """REGRESSION: a parent array naming a node absent from the graph must 422.

    `{"worker": ["planner"]}` looks fine -- "planner" IS a real role -- but the
    graph itself has no "planner" key, so the worker waits on a node that will
    never run. expand()'s own check only inspects graph KEYS, so this slips past
    it and is caught by order(); if that second check were dropped the endpoint
    would answer 200 and the browser would sit at step 0 forever with no error.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [{"worker": ["planner"]}]},
    ])
    assert status == 422
    detail = _detail(payload)
    assert "worker" in detail and "planner" in detail   # names BOTH ends of the bad edge


def test_topology_parent_naming_a_nonexistent_role_is_refused(client, base_cfg):
    """A parent that is not a role at all must 422, naming the ghost.

    Same silent-hang failure as the case above, reached by a typo in the parent
    array instead of an omitted key. The message has to name the offender or the
    user is left diffing JSON by eye.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [{"planner": [], "worker": ["ghost"]}]},
    ])
    assert status == 422
    assert "ghost" in _detail(payload)


def test_topology_cycle_is_refused_and_names_the_roles_in_it(client, base_cfg):
    """REGRESSION: a cyclic graph must 422 and name the stuck roles.

    A cycle has no topological order, so the runner would either loop forever
    burning tokens or stall. Naming the members is the difference between a
    two-second fix and bisecting a config by hand.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [{"planner": ["worker"], "worker": ["planner"]}]},
    ])
    assert status == 422
    detail = _detail(payload)
    assert "cycle" in detail.lower()
    assert "planner" in detail and "worker" in detail


def test_one_bad_topology_value_refuses_the_entire_sweep(client, base_cfg):
    """Expansion is all-or-nothing: no partial config list alongside a bad value.

    Returning the good arms and quietly dropping the broken one is worse than
    refusing, because the leaderboard then compares two topologies while the
    user believes they compared three.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [
            ["planner", "worker"],          # valid
            {"worker": ["planner"]},        # dangling parent
        ]},
    ])
    assert status == 422
    assert "configs" not in payload         # nothing partial leaked out


def test_topology_chain_naming_an_unknown_role_is_refused(client, base_cfg):
    """A chain value listing a role that does not exist must 422.

    Caught in expand() before the graph is even built, so the message can quote
    the offending chain -- more useful than the generic dangling-edge error the
    later check would produce.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [["planner", "reviewer"]]},
    ])
    assert status == 422
    assert "reviewer" in _detail(payload)


def test_empty_topology_graph_is_refused(client, base_cfg):
    """`{}` must 422 rather than expand to a config with nothing to run.

    An empty shape resolves to an empty order, which the browser renders as a
    config that finishes instantly with a perfect zero-cost score -- it wins the
    leaderboard by doing nothing.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [{}]},
    ])
    assert status == 422
    assert "empty" in _detail(payload).lower()


@pytest.mark.parametrize("bad", [[], "planner", 3, None])
def test_topology_values_must_be_a_chain_or_a_graph(client, base_cfg, bad):
    """Scalars, nulls and empty chains are all refused with one clear message.

    `values: ["planner"]` instead of `values: [["planner"]]` is a single missing
    bracket and the most common way to write this axis wrong. Coercing a string
    to a one-role chain would hide the mistake behind a sweep that ran.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "topology", "values": [bad]},
    ])
    assert status == 422
    assert "topology" in _detail(payload).lower()


# ---------------------------------------------------------------------------
# kind="substrate"
# ---------------------------------------------------------------------------

def test_substrate_wildcard_pins_every_role_to_the_same_serving_path(client, base_cfg):
    """role="*" must set `substrate` on all roles, leaving model ids untouched.

    The entire premise of this axis is holding the model id fixed while the
    serving path moves. If it rewrote the model too, a difference in the results
    would have two possible causes and the experiment would prove nothing.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "*", "values": ["direct", "bedrock"]},
    ])
    assert status == 200
    assert payload["count"] == 2
    for cfg, want in zip(payload["configs"], ["direct", "bedrock"]):
        assert cfg["roles"]["planner"]["substrate"] == want
        assert cfg["roles"]["worker"]["substrate"] == want
        assert cfg["roles"]["planner"]["model"] == "claude-opus-4-8"   # id held fixed
        assert cfg["roles"]["worker"]["model"] == "claude-sonnet-5"


def test_substrate_with_empty_role_is_treated_as_the_wildcard(client, base_cfg):
    """role="" behaves like "*", since `role` defaults to "" on SweepAxis.

    A substrate axis written without a `role` key is the natural spelling of
    "move the whole harness". Defaulting it to a role lookup instead would 422
    on the most obvious way to write the axis.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "values": ["direct"]},
    ])
    assert status == 200
    assert payload["configs"][0]["roles"]["planner"]["substrate"] == "direct"
    assert payload["configs"][0]["roles"]["worker"]["substrate"] == "direct"
    assert payload["configs"][0]["name"] == "t [on=direct]"


def test_substrate_named_role_pins_only_that_role(client, base_cfg):
    """Naming one role must leave the others on the harness default (None).

    "Move only the judge to Bedrock" is how you isolate which hop is responsible
    for a regression. Leaking the substrate onto every role turns that isolation
    experiment back into the wildcard one.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "planner", "values": ["bedrock"]},
    ])
    assert status == 200
    cfg = payload["configs"][0]
    assert cfg["roles"]["planner"]["substrate"] == "bedrock"
    assert cfg["roles"]["worker"]["substrate"] is None


def test_substrate_axis_referencing_an_undefined_substrate_is_refused(client, base_cfg):
    """REGRESSION: a substrate name not in base.substrates must 422, and say what IS defined.

    An undefined name means the browser has no base_url or wire shape to route
    with, so it silently falls back to the default endpoint -- and the sweep
    reports that Bedrock and direct are identical, because both arms ran direct.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "*", "values": ["vertex"]},
    ])
    assert status == 422
    detail = _detail(payload)
    assert "vertex" in detail
    assert "bedrock" in detail and "direct" in detail    # lists the real options


def test_substrate_axis_with_no_substrates_declared_is_refused(client, base_cfg):
    """Forgetting `substrates` entirely must 422, not expand to an unrouted config.

    base_cfg declares no substrates. The refusal is what tells the user the
    block is missing; without it the axis would appear to work and change nothing.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "substrate", "role": "*", "values": ["direct"]},
    ])
    assert status == 422
    assert "direct" in _detail(payload)


def test_substrate_axis_with_unknown_role_is_refused(client, base_cfg):
    """A substrate axis pinned to a nonexistent role must 422 naming that role.

    Same rename-drift failure as the role axis: the axis quietly applies to
    nobody, and the two arms of the sweep are byte-identical configs under two
    different names.
    """
    status, payload = expand(client, _with_substrates(base_cfg), [
        {"kind": "substrate", "role": "judge", "values": ["direct"]},
    ])
    assert status == 422
    assert "judge" in _detail(payload)


# ---------------------------------------------------------------------------
# Base-config validation still reaches through the sweep wrapper
# ---------------------------------------------------------------------------

def test_base_with_no_topology_at_all_is_refused(client, base_cfg):
    """Neither `pipeline` nor `graph` must 422 -- there is nothing to run.

    An empty topology produces an empty order, which the runner treats as a
    completed run of zero steps: a free, instant, unbeatable leaderboard entry.
    """
    base_cfg["graph"] = {}
    base_cfg["pipeline"] = []
    status, payload = expand(client, base_cfg, [])
    assert status == 422
    assert "topology" in _detail(payload).lower()


def test_base_pipeline_sugar_resolves_to_the_same_chain_as_the_graph_form(client, base_cfg):
    """`pipeline: [a, b]` and `graph: {a: [], b: [a]}` must resolve identically.

    The two spellings are the server's own duplicate description of one shape.
    If they drift, a config saved in one form and reloaded in the other runs a
    different harness under the same name.
    """
    from_graph = dict(base_cfg)
    status_g, payload_g = expand(client, from_graph, [])

    from_pipeline = dict(base_cfg, graph={}, pipeline=["planner", "worker"])
    status_p, payload_p = expand(client, from_pipeline, [])

    assert status_g == status_p == 200
    g, p = payload_g["configs"][0], payload_p["configs"][0]
    for key in ("resolved_graph", "order", "terminals", "shape", "name"):
        assert g[key] == p[key], key


def test_base_pipeline_naming_an_unknown_role_is_refused(client, base_cfg):
    """The base config is validated before any axis is applied.

    A broken base with zero axes would otherwise sail through as "one config,
    unchanged" -- the passthrough path is exactly where an invalid base is
    easiest to miss.
    """
    base_cfg["graph"] = {}
    base_cfg["pipeline"] = ["planner", "reviewer"]
    status, payload = expand(client, base_cfg, [])
    assert status == 422
    assert "reviewer" in _detail(payload)


# ---------------------------------------------------------------------------
# Suspected defects. These assert the behaviour the endpoint's own contract
# implies -- every other bad input in expand() is a named 422 -- and are xfail
# until the code agrees. strict=True so a fix turns these green loudly instead
# of leaving a stale xfail behind.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: a role axis naming a field RoleSpec does not have raises a bare "
    "ValueError out of pydantic's __setattr__ (server.py:231), which escapes the "
    "endpoint as a 500 -- and with TestClient's default settings it escapes the "
    "app entirely. The unknown-ROLE case one line above is a clean 422; the "
    "unknown-FIELD check was never written. Fix: validate ax.field against "
    "RoleSpec.model_fields before setattr and 422 with the valid field names."))
def test_role_axis_with_unknown_field_is_refused_not_crashed(client, base_cfg):
    """A misspelled `field` must 422 like every other bad axis, not 500.

    "max_token" for "max_tokens" is a one-character slip. A 500 gives the user a
    stack trace with no indication which axis is wrong, and the browser surfaces
    it as a generic network failure.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "max_token", "values": [512]},
    ])
    assert status == 422
    assert "max_token" in _detail(payload)


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: HarnessConfig does not set validate_assignment, so the setattr at "
    "server.py:231 writes axis values straight past RoleSpec's own constraints. "
    "max_tokens is declared Field(ge=1, le=64000) but 999999 expands to 200. "
    "Fix: model_config = ConfigDict(validate_assignment=True) on RoleSpec, and "
    "catch the resulting ValidationError as a 422 naming the axis."))
def test_role_axis_value_still_obeys_the_role_schema_bounds(client, base_cfg):
    """An out-of-range value must be refused at expand time, not at provider time.

    max_tokens is bounded because providers reject over-limit requests. Letting
    999999 through means every config in that arm 400s mid-sweep, after the
    other arms have already been paid for.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "max_tokens", "values": [999999]},
    ])
    assert status == 422


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: same missing validate_assignment as above, worse symptom -- a "
    "string lands in an int field and the endpoint returns 200 with "
    "roles.planner.max_tokens == 'not-a-number'. The declared schema is the "
    "thing that makes configs comparable; expand() is allowed to bypass it."))
def test_role_axis_value_still_obeys_the_role_schema_types(client, base_cfg):
    """A wrongly-typed value must not survive into an emitted config.

    The emitted config is serialized straight into a provider request body. A
    string where an int belongs is a 400 from the provider that the user has to
    trace back through two systems to reach this axis.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "max_tokens", "values": ["not-a-number"]},
    ])
    assert status == 422


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: itertools.product over an axis with values=[] yields nothing, so "
    "the endpoint returns {'count': 0, 'configs': []} with a 200. Every other "
    "malformed axis in expand() is a named 422; this one is the silent variant "
    "of exactly the failure this endpoint exists to prevent. Fix: refuse an "
    "axis with no values, naming the axis."))
def test_axis_with_no_values_is_refused_not_silently_dropped(client, base_cfg):
    """An empty `values` list must 422 rather than expand the sweep to nothing.

    A UI that adds an axis row before the user types a value posts values=[].
    Today the whole sweep -- including every other axis -- evaporates to zero
    configs and the Run button does nothing, with no error to explain why.
    """
    status, payload = expand(client, base_cfg, [
        {"kind": "role", "role": "planner", "field": "model", "values": ["a", "b"]},
        {"kind": "role", "role": "worker", "field": "model", "values": []},
    ])
    assert status == 422
