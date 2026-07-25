"""The topology model: as_graph / order / terminals.

These go at the Pydantic objects directly rather than through /sweep/expand,
because the graph is the part of the config that everything else is derived
from -- the expand endpoint, the shape label on a leaderboard row, and the
browser's own copy of the sweep all read this. A regression here is not a bad
HTTP response, it is a harness that runs its roles in the wrong order and still
returns 200 with a gradeable answer, which is exactly the failure this whole
tool exists to catch.

`order()` and friends raise HTTPException by design (the message is meant to
reach the user verbatim), so the assertions check status *and* wording: a 422
that does not say which node is at fault sends someone hunting through a config
by hand.
"""
import pytest
from fastapi import HTTPException

import server


def _cfg(**kw) -> server.HarnessConfig:
    """A config with whatever roles the topology mentions, so tests can talk
    about shape without restating a roles dict every time."""
    names = set(kw.pop("roles", []) or [])
    for r in kw.get("pipeline", []):
        names.add(r)
    for node, deps in kw.get("graph", {}).items():
        names.add(node)
    return server.HarnessConfig(
        name="t",
        roles={n: {"model": "claude-sonnet-5"} for n in sorted(names)},
        **kw,
    )


# ---------------------------------------------------------------------------
# pipeline sugar == the explicit chain
# ---------------------------------------------------------------------------


def test_pipeline_sugar_compiles_to_the_explicit_chain(base_cfg):
    """`pipeline: [a, b]` and the hand-written chain graph must be one topology.

    Both spellings are accepted, so if they ever diverge the same harness gives
    two different answers depending on how someone happened to write it down --
    and a sweep that varies topology would be comparing a notation difference
    instead of a real one.
    """
    explicit = server.HarnessConfig(**base_cfg)          # graph: {planner: [], worker: [planner]}
    sugar = server.HarnessConfig(
        name=base_cfg["name"], roles=base_cfg["roles"], pipeline=["planner", "worker"]
    )

    assert sugar.as_graph() == explicit.as_graph() == {"planner": [], "worker": ["planner"]}
    assert sugar.order() == explicit.order() == ["planner", "worker"]
    assert sugar.terminals() == explicit.terminals() == ["worker"]


def test_single_role_pipeline_is_a_topology_not_an_empty_one():
    """A one-role chain has to be a real graph, because "do I even need a
    manager?" is a topology-axis value: [["worker"], ["manager","worker"]].
    If the degenerate arm compiled to nothing, order() would 422 and the most
    interesting comparison in the sweep would be the one that never runs."""
    cfg = _cfg(pipeline=["worker"])
    assert cfg.as_graph() == {"worker": []}
    assert cfg.order() == ["worker"]
    assert cfg.terminals() == ["worker"]      # the lone node is also the answer


def test_graph_wins_when_both_pipeline_and_graph_are_set():
    """`graph` is the general form and must take precedence over the sugar.

    The topology axis writes one field and clears the other, so a stale
    `pipeline` left behind by a hand-edited config must not quietly win: that
    would run a branching harness as a line and mislabel the row.
    """
    cfg = _cfg(
        pipeline=["worker", "planner"],                  # reversed on purpose
        graph={"planner": [], "worker": ["planner"]},
    )
    assert cfg.as_graph() == {"planner": [], "worker": ["planner"]}
    # If the pipeline had won, planner would depend on worker and be the terminal.
    assert cfg.order() == ["planner", "worker"]
    assert cfg.terminals() == ["worker"]


# ---------------------------------------------------------------------------
# order() refuses bad topologies -- loudly, and by name
# ---------------------------------------------------------------------------


def test_order_refuses_a_cycle_and_names_the_nodes():
    """A cycle must 422 with the offending nodes, never spin.

    order()'s loop terminates only because "no node is ready" is treated as a
    cycle; drop that check and a mutually-dependent pair hangs the request
    thread. Naming the nodes is the difference between a fixable error and
    staring at a config.
    """
    cfg = _cfg(graph={"planner": ["worker"], "worker": ["planner"]})
    with pytest.raises(HTTPException) as e:
        cfg.order()
    assert e.value.status_code == 422
    assert "cycle" in e.value.detail
    assert "planner" in e.value.detail and "worker" in e.value.detail


def test_self_loop_is_reported_as_a_cycle():
    """A role that feeds itself is the cheapest way to write a cycle, and the
    one a hand-edited graph produces most often. It must be refused by name
    rather than treated as "no deps yet"."""
    cfg = _cfg(graph={"planner": ["planner"]})
    with pytest.raises(HTTPException) as e:
        cfg.order()
    assert e.value.status_code == 422
    assert e.value.detail == "graph has a cycle among: ['planner']"


def test_cycle_report_lists_every_unresolved_node_not_only_the_loop():
    """Pins what the message actually contains: everything still unscheduled.

    Here only planner is in the cycle -- worker is merely downstream of it --
    but both are named. That is deliberate breadth (those are all the nodes that
    will never run), and it is worth pinning: someone "tightening" the message
    to the true cycle members would silently drop nodes from the report that a
    user needs to see.
    """
    cfg = _cfg(graph={"planner": ["planner"], "worker": ["planner"]})
    with pytest.raises(HTTPException) as e:
        cfg.order()
    assert e.value.detail == "graph has a cycle among: ['planner', 'worker']"


@pytest.mark.parametrize(
    "graph, node, dep",
    [
        # the dep is a real role, but nobody put it in the graph: the classic
        # half-written graph, where planner is simply never scheduled.
        ({"worker": ["planner"]}, "worker", "planner"),
        # the dep is a typo that matches nothing at all.
        ({"planner": [], "worker": ["plannr"]}, "worker", "plannr"),
    ],
)
def test_order_refuses_an_edge_pointing_outside_the_graph(graph, node, dep):
    """An edge into a node the graph does not contain is a config bug that must
    surface here, before any provider call is paid for.

    Without this guard the dangling dep is never satisfiable, so the node it
    belongs to just never becomes ready -- and the user gets a confusing "cycle"
    (or, worse, a silently truncated run) instead of the one-line fix.
    """
    cfg = _cfg(roles=["planner", "worker"], graph=graph)
    with pytest.raises(HTTPException) as e:
        cfg.order()
    assert e.value.status_code == 422
    assert e.value.detail == f"'{node}' depends on '{dep}', which is not in the graph"


def test_order_refuses_a_graph_node_that_has_no_role():
    """A node with no RoleSpec has no model to call, so it must be refused with
    its own message -- distinct from the dangling-edge one above. The two errors
    point at opposite fixes (add a role vs. add a node), and collapsing them
    into one generic 422 sends people to the wrong end of the config."""
    cfg = server.HarnessConfig(
        name="t",
        roles={"planner": {"model": "claude-sonnet-5"}},
        graph={"planner": [], "ghost": ["planner"]},
    )
    with pytest.raises(HTTPException) as e:
        cfg.order()
    assert e.value.status_code == 422
    assert e.value.detail == "graph node 'ghost' has no matching role"


def test_order_refuses_a_config_with_no_topology_at_all():
    """Roles but no wiring is an empty harness. It must 422 saying which field
    to set, rather than returning [] and "succeeding" with a run that made no
    calls and produced no answer."""
    cfg = server.HarnessConfig(name="t", roles={"planner": {"model": "claude-sonnet-5"}})
    assert cfg.as_graph() == {}
    with pytest.raises(HTTPException) as e:
        cfg.order()
    assert e.value.status_code == 422
    assert "no topology" in e.value.detail


# ---------------------------------------------------------------------------
# terminals() -- where the answer comes from
# ---------------------------------------------------------------------------


def test_fan_in_has_exactly_one_terminal():
    """Two drafters into one judge: the judge is the harness's answer.

    terminals() is what the scoring pass grades. If an upstream drafter leaked
    into this list, the leaderboard would be scoring a draft against the rubric
    for the final answer -- a comparison that looks fine and means nothing.
    """
    cfg = _cfg(graph={"draft_a": [], "draft_b": [], "judge": ["draft_a", "draft_b"]})
    assert cfg.terminals() == ["judge"]
    assert cfg.order() == ["draft_a", "draft_b", "judge"]     # judge runs last


def test_fan_out_reports_every_unconsumed_node():
    """A branch whose arms nobody joins genuinely has several answers, and all
    of them must come back (sorted, so a config's terminals are stable across
    runs). Hard-coding "the last node" here would silently drop output."""
    cfg = _cfg(graph={"planner": [], "b": ["planner"], "c": ["planner"], "a": ["planner"]})
    assert cfg.terminals() == ["a", "b", "c"]


def test_diamond_orders_correctly_and_has_one_terminal():
    """a -> b, a -> c, b+c -> d: the canonical branch-and-rejoin.

    d must come after BOTH arms. A topological sort that only tracked one
    predecessor would schedule d early, and the judge would grade whichever
    draft happened to exist -- a wrong answer that still looks like a clean run.
    """
    cfg = _cfg(graph={"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    order = cfg.order()

    assert sorted(order) == ["a", "b", "c", "d"]              # every node, once
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")
    assert cfg.terminals() == ["d"]


def test_order_is_topological_not_alphabetical():
    """The alphabetical sort inside order() is a tie-break for reproducibility,
    not the ordering itself. Named z -> y -> x, dependencies still win; if the
    sort ever escaped its tier, every harness would run backwards for anyone
    whose role names are not conveniently in alphabetical order."""
    cfg = _cfg(graph={"z": [], "y": ["z"], "x": ["y"]})
    assert cfg.order() == ["z", "y", "x"]


def test_as_graph_hands_back_a_copy_not_the_live_config():
    """Callers (the expand endpoint, _shape_label) walk and index this dict.

    as_graph() copies the dep lists on purpose; if it aliased them, one caller
    appending an edge would mutate the stored config and every later config
    derived from it -- a sweep silently comparing topologies nobody asked for.
    """
    cfg = _cfg(graph={"planner": [], "worker": ["planner"]})
    g = cfg.as_graph()
    g["worker"].append("ghost")
    g["injected"] = []

    assert cfg.graph == {"planner": [], "worker": ["planner"]}
    assert cfg.as_graph() == {"planner": [], "worker": ["planner"]}


@pytest.mark.xfail(
    strict=True,
    reason="SUSPECTED DEFECT: a repeated role in `pipeline` compiles to a back-edge. "
           "as_graph() builds {r: [prev]} by dict comprehension, so the second "
           "occurrence overwrites the first: [planner, worker, planner] becomes "
           "{planner: [worker], worker: [planner]} -- a cycle. The user then gets "
           "'graph has a cycle among: ...' for a config that has no graph in it, "
           "which points at the wrong field entirely. A chain cannot contain a "
           "cycle; the pipeline validator should reject the duplicate by name.",
)
def test_duplicate_role_in_pipeline_is_rejected_as_a_duplicate():
    """A linear pipeline is acyclic by construction, so the only way it can fail
    is a repeated role -- and the error has to say so. Today it is reported as a
    cycle in `graph`, sending the user to a field they never wrote."""
    roles = {n: {"model": "claude-sonnet-5"} for n in ("planner", "worker")}
    try:
        cfg = server.HarnessConfig(name="t", roles=roles,
                                   pipeline=["planner", "worker", "planner"])
    except ValueError as e:                       # the fix belongs in the validator
        assert "planner" in str(e) and "duplicate" in str(e).lower()
        return
    with pytest.raises(HTTPException) as e:       # or, failing that, a truthful 422
        cfg.order()
    assert "pipeline" in e.value.detail and "duplicate" in e.value.detail.lower()
