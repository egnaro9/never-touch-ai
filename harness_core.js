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

return {
  PRICING, PRICING_VERIFIED, PRICING_SOURCE,
  baseModelId, todayISO, priceFor, costOf, round6, usd, short,
  asGraph, orderOf, terminalsOf, shapeLabel, expandSweep,
};
});
