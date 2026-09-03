/**
 * LATCH — the receiver obligation, executable.
 *
 * ~150 lines, no dependencies, runs on bare node. It is not a product and not a
 * new algorithm: it is the smallest thing that IS the rule, so an implementer
 * can read the rule instead of arguing about the prose.
 *
 * THE RULE
 *   "Could not determine" is a terminal answer. It is never "did not happen."
 *
 * WHY THIS EXISTS
 *   A payment settles. The reply is lost. Almost every implementation reads that
 *   as failure and asks again — and a conforming client signs a NEW authorization,
 *   so every replay and nonce guard correctly lets the second payment through.
 *   It is not a replay. It is an unknown collapsed into a negative.
 *
 * TWO WORLDS, GENUINELY SEPARATE
 *   A  the payer side — what the sender believes it did
 *   B  the world — what actually landed
 *
 *   A move is NOT atomic across them. `send()` debits A and puts the transfer
 *   in flight; B does not see it until `settleWorld()` runs. Between those two
 *   moments the worlds genuinely disagree, which is the entire failure class.
 *   A model where one function writes both sides cannot express the bug it
 *   claims to prevent.
 *
 * THE STATES
 *   sealed    a latch exists, nothing sent
 *   inflight  A sent, B has not confirmed — NOT a failure, NOT a success
 *   held      a witness cannot be read; terminal for this intent, never reopened
 *             by guessing
 *   open      both worlds independently say exactly one; only now may you spend
 *   broken    some world says more than one; STICKY — a later clean read never
 *             un-breaks it
 *
 * WHAT IT DOES NOT DO
 *   No network, no crypto, no persistence. `hash` is FNV-1a — a checksum for a
 *   demo chain, not a security primitive. This models control flow, nothing else.
 */

export function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

export function fresh() {
  return {
    chain: ["genesis"],
    latch: null,
    // Two worlds. Nothing writes both in one step.
    A: { balance: 100, debits: {}, readable: true },
    B: { balance: 0, credits: {}, readable: true },
    wire: [],        // transfers in flight: A has moved, B has not seen them
    files: {},
  };
}

function append(world, payload) {
  const next = hash(world.chain[world.chain.length - 1] + "|" + payload);
  world.chain.push(next);
  return next;
}

export function seal(world, amount = 40) {
  const L = world.latch;
  if (L && !L.terminal) return { ok: false, reason: "latch already sealed" };
  const key = "pay-" + hash(String(world.chain.length) + amount).slice(0, 6);
  const ticket = append(world, "seal:" + key + ":" + amount);
  world.latch = { key, amount, ticket, state: "sealed", terminal: false };
  return { ok: true, key, ticket };
}

/** What a world says about this key: zero | one | many | unknown. */
export function witness(world, side) {
  const w = world[side];
  if (!w.readable) return "unknown";
  const key = world.latch && world.latch.key;
  if (!key) return "zero";
  const n = (side === "A" ? w.debits[key] : w.credits[key]) || 0;
  return n === 0 ? "zero" : n === 1 ? "one" : "many";
}

/** A file is a story. Only the chain is evidence. */
export function writeStory(world, name, content) {
  world.files[name] = { content, sealed: false };
  return { ok: false, reason: "file is not evidence — no chain ticket" };
}

/** Debit A and put the transfer on the wire. B learns nothing yet. */
export function send(world) {
  const L = world.latch;
  if (!L) return { ok: false, reason: "no latch" };
  if (L.terminal) return { ok: false, reason: "latch is terminal: " + L.state };

  // THE RULE. An unreadable witness is not permission to send again.
  if (witness(world, "A") === "unknown" || witness(world, "B") === "unknown") {
    L.state = "held";
    reconcile(world);
    return { ok: false, reason: "unknown is terminal — not a new payment" };
  }
  if (!L.ticket || world.chain.indexOf(L.ticket) < 0) {
    L.state = "broken";
    L.terminal = true;
    return { ok: false, reason: "send without ticket" };
  }

  const n = (world.A.debits[L.key] || 0) + 1;
  world.A.debits[L.key] = n;
  world.A.balance -= L.amount;
  world.wire.push(L.key);
  append(world, "send:" + L.key);
  reconcile(world);
  if (n > 1) return { ok: false, reason: "many — a second payment for one intent" };
  return { ok: true, reason: "sent — in flight, outcome not yet known" };
}

/** The world catches up: everything on the wire lands. */
export function settleWorld(world) {
  let landed = 0;
  for (const key of world.wire.splice(0)) {
    world.B.credits[key] = (world.B.credits[key] || 0) + 1;
    world.B.balance += (world.latch && world.latch.amount) || 0;
    landed++;
  }
  reconcile(world);
  return { ok: true, landed };
}

export function dropWitness(world, side) {
  world[side].readable = false;
  reconcile(world);
}

export function restoreWitness(world, side) {
  world[side].readable = true;
  reconcile(world);
}

export function reconcile(world) {
  const L = world.latch;
  if (!L) return null;
  if (L.terminal) return L;            // broken is sticky; nothing reopens it

  const a = witness(world, "A");
  const b = witness(world, "B");

  if (a === "many" || b === "many") {
    L.state = "broken";
    L.terminal = true;                 // <- the stickiness, and it is load-bearing
    return L;
  }
  if (a === "unknown" || b === "unknown") { L.state = "held"; return L; }
  if (a === "one" && b === "one") {
    if (L.state !== "open") append(world, "open:" + L.key);
    L.state = "open";
    return L;
  }
  if (a === "one" && b === "zero") { L.state = "inflight"; return L; }
  L.state = "sealed";
  return L;
}

/** Spend is refused unless both worlds independently agree on exactly one. */
export function spend(world) {
  const L = world.latch;
  if (!L) return { ok: false, reason: "no latch" };
  if (L.state !== "open") {
    return { ok: false, reason: `latch is ${L.state} — cannot spend a story` };
  }
  return { ok: true, reason: "spent from released amount" };
}

export function status(world) {
  const L = world.latch;
  if (!L) return { state: "empty", A: "idle", B: "idle" };
  return {
    state: L.state,
    key: L.key,
    terminal: L.terminal,
    A: witness(world, "A"),
    B: witness(world, "B"),
    balances: { A: world.A.balance, B: world.B.balance },
  };
}
