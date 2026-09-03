/**
 * The mutation control for LATCH.
 *
 * Each entry breaks ONE property of latch.mjs and re-runs the UNCHANGED test
 * file. The named test must go red. A property whose mutation leaves the suite
 * green is not being tested, and this script exits non-zero so CI says so.
 *
 * This is the same standard we hold every implementation to: a battery that
 * cannot fail cannot pass.
 *
 *   node latch-mutate.mjs
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";

const here = dirname(fileURLToPath(import.meta.url));
const CORE = readFileSync(join(here, "latch.mjs"), "utf8");

const MUTATIONS = [
  {
    name: "forged file becomes evidence",
    mustBreak: [3],
    apply: (s) => s.replace(
      'return { ok: false, reason: "file is not evidence — no chain ticket" };',
      'return { ok: true, reason: "accepted" };'),
  },
  {
    name: "send proceeds on an unknown witness",
    mustBreak: [8, 9],
    apply: (s) => s.replace(
      /  if \(witness\(world, "A"\) === "unknown" \|\| witness\(world, "B"\) === "unknown"\) \{[\s\S]*?\n  \}\n/,
      ""),
  },
  {
    name: "broken is not sticky (terminal flag dropped)",
    mustBreak: [12],
    apply: (s) => s.replace(
      '    L.state = "broken";\n    L.terminal = true;                 // <- the stickiness, and it is load-bearing',
      '    L.state = "broken";'),
  },
  {
    name: "terminal latches are reconciled again",
    mustBreak: [14],
    apply: (s) => s.replace(
      "  if (L.terminal) return L;            // broken is sticky; nothing reopens it\n", ""),
  },
  {
    name: "in-flight counts as open (B never consulted)",
    mustBreak: [4, 5, 6],
    apply: (s) => s.replace(
      '  if (a === "one" && b === "one") {',
      '  if (a === "one") {'),
  },
  {
    name: "spend allowed in any state",
    mustBreak: [5],
    apply: (s) => s.replace(
      '  if (L.state !== "open") {',
      '  if (false) {'),
  },
  {
    name: "send credits B directly (worlds fused — the bug made unreachable)",
    mustBreak: [4],
    apply: (s) => s.replace(
      "  world.wire.push(L.key);",
      "  world.B.credits[L.key] = (world.B.credits[L.key] || 0) + 1;\n  world.B.balance += L.amount;"),
  },
  {
    // B still catches it after settleWorld (correctly), so test 12 stays green.
    // Only test 11 — which reads the verdict BEFORE the world catches up —
    // depends on A's own count.
    name: "a second debit is not counted",
    mustBreak: [11],
    apply: (s) => s.replace(
      "  const n = (world.A.debits[L.key] || 0) + 1;\n  world.A.debits[L.key] = n;",
      "  const n = 1;\n  world.A.debits[L.key] = 1;"),
  },
];

const dir = mkdtempSync(join(tmpdir(), "latch-mutate-"));
copyFileSync(join(here, "latch-test.mjs"), join(dir, "latch-test.mjs"));

function runWith(coreSrc) {
  writeFileSync(join(dir, "latch.mjs"), coreSrc);
  try {
    const out = execFileSync("node", ["latch-test.mjs"], { cwd: dir, encoding: "utf8" });
    return { code: 0, out };
  } catch (e) {
    return { code: e.status ?? 1, out: (e.stdout || "") + (e.stderr || "") };
  }
}

function brokenIds(out) {
  return out.split("\n").filter((l) => l.startsWith("BREAK"))
            .map((l) => parseInt(l.slice(5).trim(), 10));
}

let failures = 0;

const base = runWith(CORE);
console.log("baseline (unmutated):");
console.log("  " + (base.out.trim().split("\n").pop() || "").trim());
if (base.code !== 0) {
  console.log("  FATAL: the suite does not pass on unmutated source.");
  failures++;
}

console.log("\nmutations — each must turn its test(s) red:\n");
for (const m of MUTATIONS) {
  const mutated = m.apply(CORE);
  if (mutated === CORE) {
    console.log(`  [ERROR] "${m.name}" did not change the source — mutation is stale`);
    failures++;
    continue;
  }
  const r = runWith(mutated);
  const broke = brokenIds(r.out);
  const missed = m.mustBreak.filter((id) => !broke.includes(id));
  const ok = r.code !== 0 && missed.length === 0;
  if (!ok) failures++;
  console.log(`  [${ok ? "CAUGHT " : "ESCAPED"}] ${m.name}`);
  console.log(`             expected red: ${m.mustBreak.join(", ")}   actually red: ${broke.join(", ") || "none"}`);
  if (missed.length) console.log(`             NOT DETECTED by test(s): ${missed.join(", ")}`);
}

rmSync(dir, { recursive: true, force: true });

console.log(`\n${failures === 0
  ? "instrument valid: every mutation was caught by the test that claims that property."
  : `INSTRUMENT INVALID: ${failures} mutation(s) escaped. Those tests are not testing what they say.`}`);
process.exit(failures === 0 ? 0 : 1);
