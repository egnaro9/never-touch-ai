// Tests for the pure half of the Harness Builder.
//
// Every case here is a bug that actually shipped and was found by hand in a
// browser. That is the point of extracting harness_core.js: these were only
// reachable by driving a page, so nothing stopped them coming back.
//
//   cd "harness config test/files" && node --test tests/

const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../harness_core.js");

const {
  PRICING, PRICING_VERIFIED, baseModelId, priceFor, costOf,
  asGraph, orderOf, terminalsOf, shapeLabel, expandSweep,
} = core;

const role = (model) => ({ model, system: "", max_tokens: 900 });
const cfg = (graph, roles) => ({
  name: "t",
  roles: roles || Object.fromEntries(Object.keys(graph).map((n) => [n, role("claude-opus-4-8")])),
  pipeline: [],
  graph,
});

// ---------------------------------------------------------------------------
// PRICING
// ---------------------------------------------------------------------------
test("Sonnet 5 is introductory priced through 2026-08-31 and standard after", () => {
  // Shipped as a flat $3/$15, which overstated it by 50% for the whole intro
  // window. A price is a dated fact, so a constant is wrong on one side of it.
  assert.deepEqual(priceFor("claude-sonnet-5", "2026-07-24"), { until: "2026-08-31", in: 2, out: 10 });
  assert.deepEqual(priceFor("claude-sonnet-5", "2026-08-31"), { until: "2026-08-31", in: 2, out: 10 },
    "the last intro day is inclusive");
  assert.deepEqual(priceFor("claude-sonnet-5", "2026-09-01"), { in: 3, out: 15 });
  assert.equal(costOf("claude-sonnet-5", 1e6, 1e6, "2026-07-24"), 12);
  assert.equal(costOf("claude-sonnet-5", 1e6, 1e6, "2026-09-01"), 18);
});

test("a dated model id resolves to the same price as its undated form", () => {
  // The table was keyed on claude-haiku-4-5-20251001, so the ordinary form
  // missed the lookup and a fully priced model displayed "cost unknown".
  assert.equal(baseModelId("claude-haiku-4-5-20251001"), "claude-haiku-4-5");
  assert.deepEqual(priceFor("claude-haiku-4-5-20251001"), priceFor("claude-haiku-4-5"));
  assert.notEqual(priceFor("claude-haiku-4-5"), null);
});

test("an unknown model prices as null, never as zero", () => {
  // Returning 0 made an unknown model look free, which then won the "cheapest"
  // pill and could top a leaderboard sorted by cost.
  assert.equal(priceFor("gpt-4o-mini"), null, "delisted models stay unpriced rather than guessed");
  assert.equal(costOf("no-such-model", 1e6, 1e6), null);
  assert.notEqual(costOf("no-such-model", 1e6, 1e6), 0);
});

test("the rate card carries the date it was verified", () => {
  assert.match(PRICING_VERIFIED, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(Object.keys(PRICING).length > 5);
});

// ---------------------------------------------------------------------------
// GRAPH
// ---------------------------------------------------------------------------
test("a pipeline chain compiles to the same graph as writing it out", () => {
  const chain = { name: "t", roles: { a: role("m"), b: role("m"), c: role("m") }, pipeline: ["a", "b", "c"], graph: {} };
  assert.deepEqual(asGraph(chain), { a: [], b: ["a"], c: ["b"] });
  assert.deepEqual(orderOf(chain), ["a", "b", "c"]);
});

test("graph wins when both graph and pipeline are set", () => {
  const both = { name: "t", roles: { a: role("m"), b: role("m") }, pipeline: ["b", "a"], graph: { a: [], b: ["a"] } };
  assert.deepEqual(asGraph(both), { a: [], b: ["a"] });
});

test("a cycle is refused by name rather than hanging", () => {
  assert.throws(() => orderOf(cfg({ a: ["b"], b: ["a"] })), /cycle among: a,b/);
});

test("an edge pointing at a node not in the graph is refused", () => {
  // A topology AXIS could produce exactly this, and it used to expand cleanly
  // and then throw much later from a place where nothing caught it.
  assert.throws(() => orderOf(cfg({ a: [], b: ["ghost"] })), /'b' depends on 'ghost'/);
});

test("a graph node with no matching role is refused", () => {
  const bad = { name: "t", roles: { a: role("m") }, pipeline: [], graph: { a: [], b: [] } };
  assert.throws(() => orderOf(bad), /'b' has no matching role/);
});

test("a diamond orders correctly and has exactly one terminal", () => {
  const d = cfg({ a: [], b: ["a"], c: ["a"], d: ["b", "c"] });
  const order = orderOf(d);
  assert.ok(order.indexOf("a") < order.indexOf("b"));
  assert.ok(order.indexOf("b") < order.indexOf("d"));
  assert.ok(order.indexOf("c") < order.indexOf("d"));
  assert.deepEqual(terminalsOf(d), ["d"]);
  assert.equal(shapeLabel(d), "a→[b|c]→d", "the label shows the fan-out a chain cannot express");
});

test("a fan-out with no join has several terminals", () => {
  assert.deepEqual(terminalsOf(cfg({ a: [], b: ["a"], c: ["a"] })), ["b", "c"]);
});

// ---------------------------------------------------------------------------
// SWEEP EXPANSION
// ---------------------------------------------------------------------------
test("no axes expands to exactly the base config", () => {
  const base = cfg({ a: [], b: ["a"] });
  const out = expandSweep(base, []);
  assert.equal(out.length, 1);
  assert.equal(out[0].name, "t", "the base keeps its own name when nothing varies");
  assert.deepEqual(out[0].graph, base.graph);
});

test("a role axis produces one config per value and leaves other roles alone", () => {
  const base = cfg({ a: [], b: ["a"] });
  const out = expandSweep(base, [{ kind: "role", role: "a", field: "model", values: ["m1", "m2"] }]);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((c) => c.roles.a.model), ["m1", "m2"]);
  assert.equal(out[0].roles.b.model, "claude-opus-4-8", "the untouched role is untouched");
  assert.notEqual(out[0].name, out[1].name, "configs must be distinguishable by name");
});

test("two axes produce the full cartesian product", () => {
  const base = cfg({ a: [], b: ["a"] });
  const out = expandSweep(base, [
    { kind: "role", role: "a", field: "model", values: ["m1", "m2"] },
    { kind: "role", role: "b", field: "model", values: ["m3", "m4", "m5"] },
  ]);
  assert.equal(out.length, 6);
  assert.equal(new Set(out.map((c) => c.name)).size, 6, "every config gets a distinct name");
});

test("a topology axis can vary the shape itself", () => {
  const base = cfg({ a: [], b: ["a"], c: ["b"] });
  const out = expandSweep(base, [{ kind: "topology", values: [["a"], { a: [], b: ["a"], c: ["a"] }] }]);
  assert.equal(out.length, 2);
  assert.deepEqual(orderOf(out[0]), ["a"], "the chain form sets a one-node pipeline");
  assert.deepEqual(terminalsOf(out[1]), ["b", "c"], "the graph form can fan out");
});

test("an axis-produced dangling edge is refused AT EXPANSION", () => {
  // The defect this exists to prevent: expandSweep validated graph KEYS but not
  // parent arrays, so this expanded fine and blew up later inside runSweep,
  // outside any try/catch — the user clicked Run and got total silence.
  const base = cfg({ a: [], b: ["a"] });
  assert.throws(
    () => expandSweep(base, [{ kind: "topology", values: [{ a: [], b: ["ghost"] }] }]),
    /not valid.*depends on 'ghost'/s,
  );
});

test("an axis-introduced cycle is refused at expansion", () => {
  const base = cfg({ a: [], b: ["a"] });
  assert.throws(
    () => expandSweep(base, [{ kind: "topology", values: [{ a: ["b"], b: ["a"] }] }]),
    /not valid.*cycle/s,
  );
});

test("the refusal names which config failed, not just that one did", () => {
  // shapeLabel returns "invalid-graph" for anything broken, so labelling the
  // error with it said only that something was wrong, never which value to fix.
  const base = cfg({ a: [], b: ["a"] });
  try {
    expandSweep(base, [{ kind: "topology", values: [{ a: [], b: ["a"] }, { a: [], b: ["ghost"] }] }]);
    assert.fail("expected a refusal");
  } catch (e) {
    assert.match(e.message, /config #2 of 2/, "the position locates the offending axis value");
  }
});

test("a topology axis referencing an unknown role is refused", () => {
  const base = cfg({ a: [], b: ["a"] });
  assert.throws(() => expandSweep(base, [{ kind: "topology", values: [["a", "nope"]] }]), /unknown roles/);
});

test("a substrate axis assigns the substrate and refuses an undefined one", () => {
  const base = { ...cfg({ a: [], b: ["a"] }), substrates: { s1: { name: "s1", shape: "openai" } } };
  const out = expandSweep(base, [{ kind: "substrate", role: "a", values: ["s1"] }]);
  assert.equal(out[0].roles.a.substrate, "s1");
  assert.throws(() => expandSweep(base, [{ kind: "substrate", role: "a", values: ["nope"] }]), /not defined/);
});

test("expansion does not mutate the base config", () => {
  // Axes mutate a clone; sharing state across configs would silently make every
  // result depend on the order they were generated in.
  const base = cfg({ a: [], b: ["a"] });
  const snapshot = JSON.stringify(base);
  expandSweep(base, [{ kind: "role", role: "a", field: "model", values: ["m1", "m2"] }]);
  assert.equal(JSON.stringify(base), snapshot);
});

// ---------------------------------------------------------------------------
// PAIRED COMPARISON
// ---------------------------------------------------------------------------
const { signTestP, pairedCompare, pairedVerdict } = core;

test("the sign test matches exact binomial values", () => {
  // Hand-checkable: a clean sweep of 5 is 2 * (1/32) = 0.0625, which does NOT
  // clear p<0.05. Winning every informative task can still prove nothing.
  assert.equal(signTestP(0, 0), 1);
  assert.ok(Math.abs(signTestP(5, 0) - 0.0625) < 1e-9);
  assert.ok(Math.abs(signTestP(6, 0) - 0.03125) < 1e-9);
  assert.equal(signTestP(10, 10), 1);
  assert.equal(signTestP(3, 2), signTestP(2, 3), "direction must not change the p-value");
});

test("only tasks scored under BOTH configs count", () => {
  const r = pairedCompare({ t1: 1, t2: 1, t3: 0 }, { t1: 0, t2: 1, t9: 1 });
  assert.equal(r.shared, 2, "t3 and t9 ran under one config only");
  assert.equal(r.wins, 1);
  assert.equal(r.ties, 1);
});

test("ties are excluded from the test, not counted as evidence", () => {
  // 18 identical tasks and 2 wins is not 20 tasks of evidence; it is 2.
  const a = {}, b = {};
  for (let i = 0; i < 18; i++) { a["t" + i] = 1; b["t" + i] = 1; }
  a.w1 = 1; b.w1 = 0; a.w2 = 1; b.w2 = 0;
  const r = pairedCompare(a, b);
  assert.equal(r.shared, 20);
  assert.equal(r.ties, 18);
  assert.equal(r.informative, 2);
  assert.ok(Math.abs(r.p - 0.5) < 1e-9, "two wins out of two is a coin flip away");
  assert.equal(r.decisive, false);
});

test("a lopsided split is called, and names the winner", () => {
  const a = {}, b = {};
  for (let i = 0; i < 8; i++) { a["t" + i] = 1; b["t" + i] = 0; }
  a.x = 0; b.x = 1;
  const r = pairedCompare(a, b);
  assert.equal(r.wins, 8); assert.equal(r.losses, 1);
  assert.ok(r.p < 0.05);
  assert.equal(r.decisive, true);
  assert.equal(r.winner, "a");
  assert.match(pairedVerdict(r, "panel", "chain"), /panel is better/);
});

test("an underpowered comparison says so instead of claiming a tie", () => {
  // THE point of this feature. 3 informative tasks cannot clear p<0.05 even if
  // one config sweeps all three, so "no significant difference" would be a
  // finding the data cannot support.
  const r = pairedCompare({ t1: 1, t2: 1, t3: 1 }, { t1: 0, t2: 0, t3: 0 });
  assert.equal(r.wins, 3);
  assert.ok(r.p >= 0.05, "a sweep of 3 is p=0.25");
  assert.equal(r.underpowered, true);
  assert.equal(r.decisive, false);
  const v = pairedVerdict(r, "panel", "chain");
  assert.match(v, /cannot decide/);
  assert.match(v, /limit of the suite/);
  assert.doesNotMatch(v, /[Ii]ndistinguishable/, "never report a tie the suite could not have detected");
});

test("a genuine tie across a well-powered suite reads as indistinguishable", () => {
  const a = {}, b = {};
  for (let i = 0; i < 10; i++) { a["w" + i] = 1; b["w" + i] = 0; }
  for (let i = 0; i < 10; i++) { a["l" + i] = 0; b["l" + i] = 1; }
  const r = pairedCompare(a, b);
  assert.equal(r.informative, 20);
  assert.equal(r.underpowered, false, "20 informative tasks CAN detect a difference");
  assert.equal(r.decisive, false);
  assert.match(pairedVerdict(r, "panel", "chain"), /Indistinguishable/);
});

test("no shared tasks is reported as nothing to compare, not as a tie", () => {
  const r = pairedCompare({ t1: 1 }, { t2: 1 });
  assert.equal(r.shared, 0);
  assert.match(pairedVerdict(r), /nothing to compare/);
});

test("partial credit is compared, not just pass/fail", () => {
  const r = pairedCompare({ t1: 0.75 }, { t1: 0.5 });
  assert.equal(r.wins, 1);
  assert.equal(r.detail[0].winner, "a");
});

// ---------------------------------------------------------------------------
// COST-ADJUSTED VERDICT
// ---------------------------------------------------------------------------
const { costVerdict } = core;

const pair = (w, l, t) => {
  const a = {}, b = {};
  for (let i = 0; i < w; i++) { a["w" + i] = 1; b["w" + i] = 0; }
  for (let i = 0; i < l; i++) { a["l" + i] = 0; b["l" + i] = 1; }
  for (let i = 0; i < t; i++) { a["t" + i] = 1; b["t" + i] = 1; }
  return pairedCompare(a, b);
};

test("an expensive config that cannot be shown better buys nothing", () => {
  // The reason this exists. Ranking by mean would put the 4x config on top for
  // a difference the suite cannot detect, which reads as a trade-off when it is
  // simply a higher bill.
  const v = costVerdict(pair(3, 3, 14), 0.04, 0.01, "panel", "solo");
  assert.equal(v.worth, false);
  assert.match(v.text, /4\.0×/);
  assert.match(v.text, /buys nothing/);
});

test("underpowered is reported as buying nothing too, with the right reason", () => {
  const v = costVerdict(pair(2, 0, 18), 0.04, 0.01, "panel", "solo");
  assert.equal(v.worth, false);
  assert.match(v.text, /cannot decide/, "not 'indistinguishable' — the suite never had the power");
});

test("when the dearer config genuinely wins, the price of the win is stated", () => {
  const v = costVerdict(pair(8, 1, 11), 0.04, 0.01, "panel", "solo");
  assert.equal(v.worth, null, "the tool prices the trade-off; it does not make the call");
  assert.match(v.text, /wins by 7 tasks/);
  assert.match(v.text, /per extra task won/);
  assert.match(v.text, /your call, not the tool's/);
});

test("a cheaper config that also wins is not framed as a trade-off", () => {
  const v = costVerdict(pair(0, 8, 12), 0.04, 0.01, "panel", "solo");
  assert.equal(v.worth, true);
  assert.match(v.text, /both better AND/);
  assert.match(v.text, /no trade-off/);
});

test("an unpriced side refuses to compute a trade-off rather than assuming zero", () => {
  // Same rule as costOf returning null: a missing price is unknown, not free.
  for (const [ca, cb] of [[null, 0.01], [0.04, null], [0, 0.01], [NaN, 0.01]]) {
    const v = costVerdict(pair(8, 1, 11), ca, cb, "panel", "solo");
    assert.equal(v.known, false, `costs ${ca}/${cb} must not yield a verdict`);
    assert.match(v.text, /cost unknown/);
  }
});

test("a free run reports no cost to weigh, not unknown cost", () => {
  // A mock sweep spends a measured zero. Calling that "unknown" turns a
  // measurement into an absence of one, which is the error this file exists to
  // avoid in the other direction.
  const v = costVerdict(pair(0, 0, 20), 0, 0, "#1", "#2");
  assert.equal(v.known, true);
  assert.match(v.text, /without spending anything/);
  assert.doesNotMatch(v.text, /unknown/);
  // but a genuinely unpriced side is still unknown
  assert.equal(costVerdict(pair(8, 1, 11), null, 0, "#1", "#2").known, false);
});

// ---------------------------------------------------------------------------
// RENAME
// ---------------------------------------------------------------------------
const { renameRole, renameRoleInAxes } = core;

test("renaming updates the role, its graph key, AND every reference to it", () => {
  // The classic corruption: updating two of the three places silently orphans a
  // node or invents a dangling edge that only surfaces at run time.
  const base = {
    name: "t",
    roles: { planner: role("m1"), worker: role("m2"), judge: role("m3") },
    pipeline: [],
    graph: { planner: [], worker: ["planner"], judge: ["planner", "worker"] },
  };
  renameRole(base, "planner", "manager");
  assert.deepEqual(Object.keys(base.roles), ["manager", "worker", "judge"], "position preserved");
  assert.deepEqual(base.graph, { manager: [], worker: ["manager"], judge: ["manager", "worker"] });
  assert.deepEqual(orderOf(base), ["manager", "worker", "judge"], "still a runnable graph");
});

test("the role body survives a rename byte for byte", () => {
  const body = { model: "claude-opus-4-8", system: "a hand written prompt", max_tokens: 421, substrate: "s1" };
  const base = { name: "t", roles: { a: body }, pipeline: [], graph: { a: [] } };
  renameRole(base, "a", "b");
  assert.deepEqual(base.roles.b, body);
  assert.equal(base.roles.b.system, "a hand written prompt");
});

test("a pipeline chain is renamed too", () => {
  const base = { name: "t", roles: { a: role("m"), b: role("m") }, pipeline: ["a", "b"], graph: {} };
  renameRole(base, "a", "z");
  assert.deepEqual(base.pipeline, ["z", "b"]);
  assert.deepEqual(asGraph(base), { z: [], b: ["z"] });
});

test("renaming onto an existing name is refused, not merged", () => {
  const base = { name: "t", roles: { a: role("m"), b: role("m") }, pipeline: [], graph: { a: [], b: ["a"] } };
  assert.throws(() => renameRole(base, "a", "b"), /already exists/);
  assert.deepEqual(Object.keys(base.roles), ["a", "b"], "nothing changed on refusal");
});

test("empty and malformed names are refused", () => {
  const mk = () => ({ name: "t", roles: { a: role("m") }, pipeline: [], graph: { a: [] } });
  assert.throws(() => renameRole(mk(), "a", "   "), /needs a name/);
  assert.throws(() => renameRole(mk(), "a", "2fast"), /must start with a letter/);
  assert.throws(() => renameRole(mk(), "a", "has space"), /letters, digits or _/);
  // '|' used to be an edge delimiter, and a name containing one cut the wrong edge
  assert.throws(() => renameRole(mk(), "a", "a|b"), /letters, digits or _/);
});

test("renaming a role that does not exist is refused", () => {
  const base = { name: "t", roles: { a: role("m") }, pipeline: [], graph: { a: [] } };
  assert.throws(() => renameRole(base, "ghost", "x"), /no role named 'ghost'/);
});

test("axes are renamed too, or a working sweep breaks", () => {
  // An axis left pointing at the old name turns into "axis references unknown
  // role" at expansion — a rename that breaks the sweep it was meant to set up.
  const axes = [
    { kind: "role", role: "planner", field: "model", values: ["m1", "m2"] },
    { kind: "topology", values: [["planner", "worker"], { planner: [], worker: ["planner"] }] },
  ];
  renameRoleInAxes(axes, "planner", "manager");
  assert.equal(axes[0].role, "manager");
  assert.deepEqual(axes[1].values[0], ["manager", "worker"]);
  assert.deepEqual(axes[1].values[1], { manager: [], worker: ["manager"] });
});

test("a rename leaves the whole config still expandable", () => {
  const base = {
    name: "t", roles: { planner: role("m"), worker: role("m") },
    pipeline: [], graph: { planner: [], worker: ["planner"] },
  };
  const axes = [{ kind: "role", role: "planner", field: "model", values: ["m1", "m2"] }];
  renameRole(base, "planner", "manager");
  renameRoleInAxes(axes, "planner", "manager");
  const out = expandSweep(base, axes);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((c) => c.roles.manager.model), ["m1", "m2"]);
});
