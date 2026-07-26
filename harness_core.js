// ===========================================================================
// harness_core.js — the pure part of the Harness Builder.
//
// Pricing and graph logic with no DOM, no network and no state, extracted so it
// can be TESTED. It previously lived inline in sweep.html, where nothing could
// import it and the only way to check it was to drive a browser by hand — which
// is exactly how a broken spend cap and a silently-unrunnable topology both
// shipped.
//
// Loads as a plain <script> in the browser (assigns onto globalThis, so every
// existing call site keeps working unchanged) and as a CommonJS module under
// `node --test`.
// ===========================================================================
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
"use strict";

// ---------------------------------------------------------------------------
// Pricing — USD per 1,000,000 tokens.
// Verified against platform.claude.com/docs/en/about-claude/pricing on the date
// below. That date is shown in the UI: a price table with no "as of" is a claim
// with no expiry, and this one WILL go stale.
//
// A price is either a constant or a dated schedule. Sonnet 5 is the reason the
// schedule exists — it runs $2/$10 introductory through 2026-08-31 and $3/$15
// after, so any single constant is wrong on one side of that date. This file
// previously hardcoded the post-intro $3/$15 and overstated Sonnet 5 by 50%.
// ---------------------------------------------------------------------------
const PRICING_VERIFIED = "2026-07-24";
const PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing";
const PRICING = {
  "claude-fable-5":    { in: 10, out: 50 },
  "claude-opus-5":     { in: 5,  out: 25 },
  "claude-opus-4-8":   { in: 5,  out: 25 },
  "claude-opus-4-7":   { in: 5,  out: 25 },
  "claude-opus-4-6":   { in: 5,  out: 25 },
  "claude-sonnet-5":   [{ until: "2026-08-31", in: 2, out: 10 }, { in: 3, out: 15 }],
  "claude-sonnet-4-6": { in: 3,  out: 15 },
  "claude-haiku-4-5":  { in: 1,  out: 5  },

  // OpenAI, standard tier — verified against developers.openai.com/api/docs/pricing
  // on the same date. Cross-vendor comparison is the point of the substrate axis,
  // and it is worth nothing if one vendor's column reads "cost unknown".
  // gpt-4o-mini is deliberately absent: it no longer appears on that page, and a
  // remembered price for a delisted model is exactly the kind of number this tool
  // refuses to invent.
  "gpt-5.6-sol":       { in: 5,    out: 30   },
  "gpt-5.6-terra":     { in: 2.5,  out: 15   },
  "gpt-5.6-luna":      { in: 1,    out: 6    },
  "gpt-5.5":           { in: 5,    out: 30   },
  "gpt-5.4":           { in: 2.5,  out: 15   },
  "gpt-5.4-mini":      { in: 0.75, out: 4.5  },
  "gpt-5.4-nano":      { in: 0.2,  out: 1.25 },
};

// Model ids ship in dated and undated forms — claude-haiku-4-5-20251001 and
// claude-haiku-4-5 are the same model at the same price. Key the table on the
// undated form and strip the suffix on lookup, so a real model never falls into
// the "cost unknown" bucket over a naming detail.
const baseModelId = (m) => String(m).replace(/-\d{8}$/, "");
const todayISO = () => new Date().toISOString().slice(0, 10);

// Resolves the rate card in effect on `when` (default: today).
function priceFor(model, when) {
  const p = PRICING[baseModelId(model)];
  if (!p) return null;
  if (!Array.isArray(p)) return p;
  const day = when || todayISO();
  return p.find((tier) => !tier.until || day <= tier.until) || p[p.length - 1];
}

// Returns null for an unpriced model rather than 0. Returning 0 made an unknown
// model look free, which then won the "cheapest" pill and could top a leaderboard
// sorted by cost — a made-up number beating a measured one.
function costOf(model, inTok, outTok, when) {
  const p = priceFor(model, when);
  if (!p) return null;
  return (inTok / 1e6) * p.in + (outTok / 1e6) * p.out;
}
const round6 = (x) => Math.round(x * 1e6) / 1e6;
const usd = (x) => "$" + x.toFixed(x < 0.01 ? 5 : 4);
const short = (v) => { const s = String(v); return s.length <= 24 ? s : s.slice(0, 21) + "..."; };

function asGraph(cfg) {
  if (cfg.graph && Object.keys(cfg.graph).length)
    return Object.fromEntries(Object.entries(cfg.graph).map(([k, v]) => [k, [...v]]));
  const p = cfg.pipeline || [];
  return Object.fromEntries(p.map((r, i) => [r, i ? [p[i - 1]] : []]));
}

// Topological order, refusing cycles by name rather than hanging.
function orderOf(cfg) {
  const g = asGraph(cfg);
  if (!Object.keys(g).length) throw new Error("config has no topology: set `pipeline` or `graph`");
  for (const [n, deps] of Object.entries(g)) {
    if (!cfg.roles[n]) throw new Error(`graph node '${n}' has no matching role`);
    for (const d of deps) if (!(d in g)) throw new Error(`'${n}' depends on '${d}', not in the graph`);
  }
  const seen = new Set(), out = [];
  while (out.length < Object.keys(g).length) {
    const ready = Object.keys(g).filter((n) => !seen.has(n) && g[n].every((d) => seen.has(d))).sort();
    if (!ready.length) throw new Error(`graph has a cycle among: ${Object.keys(g).filter(n=>!seen.has(n)).sort()}`);
    ready.forEach((n) => { out.push(n); seen.add(n); });
  }
  return out;
}
function terminalsOf(cfg) {
  const g = asGraph(cfg);
  const fed = new Set(Object.values(g).flat());
  return Object.keys(g).filter((n) => !fed.has(n)).sort();
}

function shapeLabel(cfg) {
  let g, order;
  try { g = asGraph(cfg); order = orderOf(cfg); } catch { return "invalid-graph"; }
  const placed = new Set(), tiers = [];
  while (placed.size < order.length) {
    const tier = order.filter((n) => !placed.has(n) && g[n].every((d) => placed.has(d))).sort();
    if (!tier.length) break;
    tiers.push(tier); tier.forEach((n) => placed.add(n));
  }
  return tiers.map((t) => (t.length === 1 ? t[0] : "[" + t.join("|") + "]")).join("→");
}

function expandSweep(base, axes) {
  if (!axes || !axes.length) return [structuredClone(base)];
  let combos = [[]];
  for (const ax of axes) {
    combos = combos.flatMap((c) => ax.values.map((v) => [...c, { ax, value: v }]));
  }
  return combos.map((combo, comboIdx) => {
    const cfg = structuredClone(base);
    const bits = [];
    for (const { ax, value } of combo) {
      const kind = ax.kind || "role";

      if (kind === "topology") {
        // A value is a chain (array of roles) or a shape (role -> its inputs).
        // The object form is the one that can branch.
        if (value && !Array.isArray(value) && typeof value === "object") {
          const nodes = Object.keys(value);
          if (!nodes.length) throw new Error("topology graph cannot be empty");
          const missing = nodes.filter((r) => !cfg.roles[r]);
          if (missing.length)
            throw new Error(`topology graph references unknown roles: ${missing}`);
          cfg.graph = Object.fromEntries(nodes.map((k) => [k, [...value[k]]]));
          cfg.pipeline = [];
          bits.push(shapeLabel(cfg));
        } else if (Array.isArray(value) && value.length) {
          const missing = value.filter((r) => !cfg.roles[r]);
          if (missing.length)
            throw new Error(`topology [${value}] references unknown roles: ${missing}`);
          cfg.pipeline = [...value];
          cfg.graph = {};
          bits.push(value.join("→"));
        } else {
          throw new Error("topology values must be a non-empty chain or graph");
        }

      } else if (kind === "substrate") {
        if (!cfg.substrates || !cfg.substrates[value])
          throw new Error(`substrate '${value}' is not defined in base.substrates`);
        const targets = (!ax.role || ax.role === "*") ? Object.keys(cfg.roles) : [ax.role];
        for (const r of targets) {
          if (!cfg.roles[r]) throw new Error(`substrate axis references unknown role '${r}'`);
          cfg.roles[r].substrate = value;
        }
        bits.push((!ax.role || ax.role === "*") ? `on=${value}` : `${ax.role}@${value}`);

      } else {
        if (!cfg.roles[ax.role]) throw new Error(`sweep axis references unknown role '${ax.role}'`);
        cfg.roles[ax.role][ax.field] = value;
        bits.push(`${ax.role}.${ax.field}=${short(value)}`);
      }
    }
    // Validate what the AXES produced, not only what the user typed. parseConfig
    // checks `base`; it never sees a topology axis value. And the topology branch
    // above only checks that graph KEYS resolve to roles — a parent naming a node
    // that is not in the graph sailed through here and then threw much later, from
    // runSweep's pre-flight, where nothing catches it: the user clicked Run and got
    // silence. Refuse here instead, by name, with the axis label attached.
    // Name it by POSITION, not by label: shapeLabel returns the string
    // "invalid-graph" for anything broken, so labelling the error with it told
    // you only that something was wrong, never which axis value to go fix.
    try { orderOf(cfg); }
    catch (e) {
      throw new Error(`config #${comboIdx + 1} of ${combos.length} ` +
        `(${combo.map(({ ax, value }) => `${ax.kind || "role"}=${short(value)}`).join(", ")}) ` +
        `is not valid: ${e.message}`);
    }

    cfg.name = `${base.name} [${bits.join(", ")}]`;
    return cfg;
  });
}

// ===========================================================================
// PAIRED COMPARISON — is one harness actually better, or is that one task?
//
// A 20-task suite moves in 5-point steps, so "85% vs 80%" is a single task and
// comparing two independent means at that sample size mostly measures noise.
// Both harnesses run the SAME tasks, so the far more sensitive question is
// per-task: on how many tasks did A beat B, and could that split have come from
// a coin?
//
// The sign test is the honest tool here. It assumes almost nothing — no
// normality, no equal variance, no interval scale — only that under the null
// each disagreement is equally likely to fall either way. Ties carry no
// information about direction and are excluded, which is the standard treatment
// and also why a 20-task suite can end up with very few informative tasks.
// ===========================================================================

function choose(n, k) {
  if (k < 0 || k > n) return 0;
  k = Math.min(k, n - k);
  let r = 1;
  for (let i = 1; i <= k; i++) r = (r * (n - k + i)) / i;
  return r;
}

// Two-sided exact sign test. Returns the probability of a split at least this
// lopsided arising by chance from a fair coin.
function signTestP(wins, losses) {
  const n = wins + losses;
  if (n === 0) return 1;
  const hi = Math.max(wins, losses);
  let tail = 0;
  for (let k = hi; k <= n; k++) tail += choose(n, k);
  return Math.min(1, (2 * tail) / Math.pow(2, n));
}

// scoresA / scoresB: { taskId: score }. Only tasks present in BOTH count — a
// task one config never ran tells you nothing about which is better.
function pairedCompare(scoresA, scoresB, opts) {
  const eps = (opts && opts.eps) != null ? opts.eps : 1e-9;
  const alpha = (opts && opts.alpha) != null ? opts.alpha : 0.05;
  const shared = Object.keys(scoresA).filter((t) => t in scoresB).sort();
  let wins = 0, losses = 0, ties = 0;
  const detail = [];
  for (const t of shared) {
    const d = scoresA[t] - scoresB[t];
    if (Math.abs(d) <= eps) { ties++; detail.push({ task: t, winner: null, delta: 0 }); }
    else if (d > 0) { wins++; detail.push({ task: t, winner: "a", delta: d }); }
    else { losses++; detail.push({ task: t, winner: "b", delta: d }); }
  }
  const p = signTestP(wins, losses);
  const decisive = p < alpha;
  return {
    shared: shared.length, wins, losses, ties,
    informative: wins + losses,
    p,
    decisive,
    winner: decisive ? (wins > losses ? "a" : "b") : null,
    detail,
    // The number that keeps this honest: with this many informative tasks, the
    // most lopsided possible split. If even a clean sweep cannot clear alpha,
    // the suite CANNOT answer the question and saying "no difference" would be
    // a claim the data cannot support.
    minP: signTestP(wins + losses, 0),
    underpowered: signTestP(wins + losses, 0) >= alpha,
  };
}

// Plain-language reading. Refuses to call a tie a tie when the suite was never
// capable of detecting a difference in the first place.
function pairedVerdict(r, nameA, nameB) {
  const a = nameA || "A", b = nameB || "B";
  if (r.shared === 0) return "No task was scored under both configs, so there is nothing to compare.";
  if (r.informative === 0)
    return `${a} and ${b} scored identically on all ${r.shared} shared tasks — no task separates them.`;
  if (r.underpowered)
    return `Only ${r.informative} task${r.informative === 1 ? "" : "s"} separated ${a} and ${b}. ` +
           `Even a clean sweep of ${r.informative} could not clear p<0.05, so this suite cannot ` +
           `decide between them — that is a limit of the suite, not a finding about the harnesses.`;
  if (!r.decisive)
    return `${a} won ${r.wins}, ${b} won ${r.losses}, ${r.ties} tied (p=${r.p.toFixed(3)}). ` +
           `Indistinguishable on this suite.`;
  const win = r.winner === "a" ? a : b;
  const hi = Math.max(r.wins, r.losses), lo = Math.min(r.wins, r.losses);
  return `${win} is better: won ${hi}, lost ${lo}, ${r.ties} tied (p=${r.p.toFixed(3)}).`;
}


// ===========================================================================
// COST-ADJUSTED VERDICT — is being better worth what it costs?
//
// "Correctness first, cost second" answers the wrong question when the win is
// not established. A config that is 4x the price and cannot be SHOWN better is
// not a trade-off, it is just more expensive, and the ranking should say so
// rather than quietly placing it above on a difference the suite cannot detect.
// ===========================================================================

function costVerdict(r, costA, costB, nameA, nameB) {
  const a = nameA || "A", b = nameB || "B";
  const bothFree = costA === 0 && costB === 0;
  if (bothFree) {
    // A mock run spends nothing, and reporting that as "cost unknown" is wrong
    // in the same direction the rest of this file refuses: it turns a MEASURED
    // zero into an absence of information.
    return { known: true, worth: null, ratio: 1,
             text: "both ran without spending anything, so there is no cost to weigh." };
  }
  const known = Number.isFinite(costA) && Number.isFinite(costB) && costA > 0 && costB > 0;
  if (!known) return { known: false, text: "cost unknown for one side — no trade-off can be computed." };

  const dearer = costA > costB ? "a" : "b";
  const ratio = Math.max(costA, costB) / Math.min(costA, costB);
  const dearName = dearer === "a" ? a : b;
  const cheapName = dearer === "a" ? b : a;
  const ratioTxt = ratio.toFixed(ratio >= 10 ? 0 : 1) + "\u00d7";

  // Not decisive: the expensive one has not earned anything, whichever it is.
  if (!r.decisive) {
    const why = r.underpowered
      ? "the suite cannot decide between them"
      : "they are indistinguishable on this suite";
    return {
      known: true, worth: false, ratio,
      text: `${dearName} costs ${ratioTxt} more than ${cheapName} and ${why} — ` +
            `on this evidence the extra spend buys nothing.`,
    };
  }

  const winner = r.winner === "a" ? a : b;
  const margin = Math.abs(r.wins - r.losses);
  // The cheaper one won: no trade-off at all, it is simply better.
  if (winner === cheapName) {
    return {
      known: true, worth: true, ratio,
      text: `${cheapName} is both better AND ${ratioTxt} cheaper than ${dearName} — ` +
            `there is no trade-off to weigh.`,
    };
  }
  // The dearer one won: state the price of the win and let the reader judge.
  const perWin = (Math.max(costA, costB) - Math.min(costA, costB)) / margin;
  return {
    known: true, worth: null, ratio,
    text: `${dearName} wins by ${margin} task${margin === 1 ? "" : "s"} and costs ${ratioTxt} more ` +
          `(${usd(perWin)} per extra task won). Whether that is worth it is your call, not the tool's.`,
  };
}


// ===========================================================================
// RENAME — a role name is a graph key, so renaming is topology work.
//
// This is where box-and-arrow editors classically corrupt data: the name is
// referenced from three places (the roles map, the graph's own key, and every
// OTHER node's parent list) and missing any one of them silently orphans a
// node or invents a dangling edge. Doing it in one pure function, over the
// whole config at once, is what makes that hard to get wrong.
//
// Mutates a config in place and returns it. Throws on anything ambiguous
// rather than guessing.
// ===========================================================================
function renameRole(base, from, to) {
  const name = String(to || "").trim();
  if (!name) throw new Error("a role needs a name");
  if (name === from) return base;
  if (!base.roles || !(from in base.roles)) throw new Error(`no role named '${from}'`);
  if (name in base.roles) throw new Error(`'${name}' already exists`);
  // The name ends up as a JSON key and inside <<tags>> in a fan-in prompt, so
  // keep it to something that survives both without quoting games.
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name))
    throw new Error(`'${name}' must start with a letter and contain only letters, digits or _`);

  // Rebuild rather than delete+add, so the role keeps its position in the file.
  // A rename that reshuffles the config makes the diff unreadable.
  base.roles = Object.fromEntries(
    Object.entries(base.roles).map(([k, v]) => [k === from ? name : k, v]));

  if (base.graph) {
    base.graph = Object.fromEntries(
      Object.entries(base.graph).map(([k, deps]) => [
        k === from ? name : k,
        (deps || []).map((d) => (d === from ? name : d)),
      ]));
  }
  if (Array.isArray(base.pipeline))
    base.pipeline = base.pipeline.map((r) => (r === from ? name : r));

  // Axes reference roles by name too. A rename that leaves an axis pointing at
  // the old name turns a working sweep into "axis references unknown role".
  return base;
}

// Axes live beside `base`, so they are renamed separately and explicitly.
function renameRoleInAxes(axes, from, to) {
  for (const ax of axes || []) {
    if (ax.role === from) ax.role = to;
    if ((ax.kind || "role") === "topology" && Array.isArray(ax.values)) {
      ax.values = ax.values.map((v) => {
        if (Array.isArray(v)) return v.map((r) => (r === from ? to : r));
        if (v && typeof v === "object")
          return Object.fromEntries(Object.entries(v).map(([k, deps]) => [
            k === from ? to : k, (deps || []).map((d) => (d === from ? to : d))]));
        return v;
      });
    }
  }
  return axes;
}


// ---------------------------------------------------------------------------
// THE ASSISTANT'S SCORE GUARD.
//
// The page claims no model produces a score here. For the CONFIG path that was
// already true by construction: a proposal has nowhere to put a score and is
// re-validated through expandSweep/orderOf before it is offered.
//
// For the assistant's PROSE it was not true. It rested on one line of system
// prompt -- "You NEVER grade, score or judge output" -- and a prompt is a
// request, not an enforcement. The model could answer "the panel is about 8/10"
// and the page would render it next to a banner promising it had not.
//
// The assistant IS allowed to read and repeat a real score; that is the whole
// point of "explain the last run". So the invariant is not "no numbers". It is:
//
//     every score-shaped number in the reply must appear in the run data
//
// which is the same discipline as grounding a summary in retrieved context --
// what it says has to be traceable to something that was actually computed.
// Anything else is flagged in the UI and named, rather than quietly rendered.
//
// Deliberately narrow. Counts and ratios ("4 calls per task", "22x the cost")
// are not score-shaped and are not touched.
// ---------------------------------------------------------------------------
const SCORE_PATTERNS = [
  // 85%, 85.5 %
  /(\d{1,3}(?:\.\d+)?)\s*%/g,
  // 8/10, 85/100
  /(\d{1,3}(?:\.\d+)?)\s*\/\s*(?:10|100)\b/g,
  // 8 out of 10
  /(\d{1,3}(?:\.\d+)?)\s+out of\s+(?:10|100)\b/g,
  // scored 85, scores 0.85, rated 8
  /\b(?:scored|scores|rated|rates|grades?)\s+(?:it\s+)?(?:a\s+)?(\d{1,3}(?:\.\d+)?)\b/gi,
];

// Pull every score-shaped number out of prose. Returns [{raw, value}].
function scoreClaims(text) {
  const s = String(text || "");
  // Keyed by value: the patterns overlap on purpose ("scored 62%" matches both
  // the percent form and the verb form), and one number the reader sees once
  // should be one claim, not two. Longest raw wins so the flag quotes the fuller
  // phrase back.
  const byValue = new Map();
  for (const re of SCORE_PATTERNS) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(s)) !== null) {
      const value = Number(m[1]);
      if (!Number.isFinite(value)) continue;
      const raw = m[0].trim();
      const prev = byValue.get(value);
      if (!prev || raw.length > prev.raw.length) byValue.set(value, { raw, value });
    }
  }
  return [...byValue.values()];
}

// Every number the deterministic side actually produced, as a flat list. Scores
// are accepted in either scale (0.95 and 95 are the same claim to a reader).
function groundedNumbers(runData) {
  const nums = new Set();
  const add = (n) => {
    if (typeof n !== "number" || !Number.isFinite(n)) return;
    nums.add(n);
    if (n > 0 && n <= 1) nums.add(n * 100);
    if (n > 1 && n <= 100) nums.add(n / 100);
  };
  const walk = (v, depth) => {
    if (depth > 6 || v == null) return;
    if (typeof v === "number") return add(v);
    // A string is accepted so the run SUMMARY -- the exact text the assistant
    // was handed -- can be the grounding source. Then "is this number real?"
    // becomes "was it in what I gave you?", which is the honest question.
    if (typeof v === "string") {
      const m = v.match(/\d+(?:\.\d+)?/g) || [];
      return m.forEach((x) => add(Number(x)));
    }
    if (Array.isArray(v)) return v.forEach((x) => walk(x, depth + 1));
    if (typeof v === "object") return Object.values(v).forEach((x) => walk(x, depth + 1));
  };
  walk(runData, 0);
  return nums;
}

// The check itself. `runData` is whatever the last sweep produced, or null when
// nothing has run -- in which case there is no score to repeat, so every
// score-shaped claim is ungrounded by definition.
function ungroundedScores(replyText, runData, tolerance) {
  const tol = typeof tolerance === "number" ? tolerance : 0.5;
  const claims = scoreClaims(replyText);
  if (!claims.length) return [];
  const grounded = groundedNumbers(runData);
  return claims.filter((c) => {
    for (const g of grounded) if (Math.abs(g - c.value) <= tol) return false;
    return true;
  });
}

return {
  PRICING, PRICING_VERIFIED, PRICING_SOURCE,
  baseModelId, todayISO, priceFor, costOf, round6, usd, short,
  asGraph, orderOf, terminalsOf, shapeLabel, expandSweep,
  choose, signTestP, pairedCompare, pairedVerdict, costVerdict,
  renameRole, renameRoleInAxes,
  scoreClaims, groundedNumbers, ungroundedScores,
};
});
