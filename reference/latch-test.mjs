/**
 * LATCH conformance — and its own mutation control.
 *
 * Every test below is paired with a mutation in latch-mutate.mjs that destroys
 * exactly the property it claims to check. If a test still passes under its own
 * mutation, the test is lying and CI goes red. Run `node latch-mutate.mjs`.
 *
 * There is deliberately no test that merely counts the other tests. A test whose
 * only content is "the previous ones passed" cannot fail on its own and inflates
 * the score for free.
 */
import {
  dropWitness, fresh, restoreWitness, seal, send, settleWorld, spend, status, writeStory,
} from "./latch.mjs";

const cases = [];
function test(id, title, fn) {
  let pass = false, detail = "";
  try {
    const out = fn(fresh());
    pass = !!out.pass;
    detail = out.detail || "";
  } catch (e) {
    detail = "threw: " + String((e && e.message) || e);
  }
  cases.push({ id, title, pass, detail });
}

test(1, "no latch, no send", (w) => {
  const r = send(w);
  return { pass: !r.ok && status(w).state === "empty", detail: r.reason };
});

test(2, "a file is a story, never evidence", (w) => {
  seal(w);
  writeStory(w, "receipt.txt", "paid");
  const s = status(w);
  return { pass: s.state === "sealed" && s.A === "zero", detail: "forged file ignored" };
});

test(3, "a forged postmortem is refused", (w) => {
  seal(w);
  const r = writeStory(w, "postmortem.md", "reviewed");
  return { pass: !r.ok, detail: r.reason };
});

test(4, "IN FLIGHT is not success: A sent, B has not seen it", (w) => {
  seal(w);
  const r = send(w);
  const s = status(w);
  return {
    pass: r.ok && s.state === "inflight" && s.A === "one" && s.B === "zero"
          && s.balances.A === 60 && s.balances.B === 0,
    detail: `${s.state} A=${s.A} B=${s.B} bal=${s.balances.A}/${s.balances.B}`,
  };
});

test(5, "in flight cannot be spent", (w) => {
  seal(w);
  send(w);
  const r = spend(w);
  return { pass: !r.ok && status(w).state === "inflight", detail: r.reason };
});

test(6, "open only when BOTH worlds say one — B's agreement is what flips it", (w) => {
  seal(w);
  send(w);
  const before = status(w);            // A=one, B=zero — must NOT be open
  settleWorld(w);
  const after = status(w);             // B now agrees — only now open
  return {
    pass: before.state !== "open" && before.A === "one" && before.B === "zero"
          && after.state === "open" && after.A === "one" && after.B === "one"
          && after.balances.A === 60 && after.balances.B === 40,
    detail: `before=${before.state}(B=${before.B}) after=${after.state}(B=${after.B})`,
  };
});

test(7, "spend allowed once open", (w) => {
  seal(w);
  send(w);
  settleWorld(w);
  const r = spend(w);
  return { pass: r.ok, detail: r.reason };
});

test(8, "unknown holds, and does not send", (w) => {
  seal(w);
  dropWitness(w, "A");
  const r = send(w);
  const s = status(w);
  return {
    pass: !r.ok && s.state === "held" && s.A === "unknown" && s.balances.A === 100,
    detail: r.reason,
  };
});

test(9, "THE HARD CASE: sent, then the answer is lost, then a retry — holds", (w) => {
  seal(w);
  send(w);                       // money left A
  dropWitness(w, "B");           // we can no longer read whether it landed
  const r = send(w);             // the client retries
  const s = status(w);
  return {
    pass: !r.ok && s.state === "held" && s.balances.A === 60,
    detail: `${r.reason} | A=${s.balances.A} ${s.state}`,
  };
});

test(10, "a restored witness resolves the hold truthfully, not by guessing", (w) => {
  seal(w);
  send(w);
  dropWitness(w, "B");
  send(w);                       // refused while held
  settleWorld(w);                // the transfer actually lands
  restoreWitness(w, "B");
  const s = status(w);
  return {
    pass: s.state === "open" && s.B === "one" && s.balances.B === 40,
    detail: `${s.state} B=${s.B}`,
  };
});

test(11, "two sends for one intent is BROKEN", (w) => {
  seal(w);
  send(w);
  const second = send(w);
  const s = status(w);
  return { pass: !second.ok && s.state === "broken", detail: `${s.state} A=${s.A}` };
});

test(12, "broken is STICKY: a later clean world never reopens it", (w) => {
  seal(w);
  send(w);
  send(w);                       // broken
  settleWorld(w);                // world catches up
  const afterSettle = status(w);
  const spendAttempt = spend(w);
  const s = status(w);
  return {
    pass: afterSettle.state === "broken" && s.state === "broken" && s.terminal === true
          && !spendAttempt.ok,
    detail: `${s.state} terminal=${s.terminal}`,
  };
});

test(14, "broken cannot be LAUNDERED into held by losing a witness afterwards", (w) => {
  seal(w);
  send(w);
  send(w);                       // broken, terminal
  dropWitness(w, "A");           // now the world goes dark
  const s = status(w);           // 'unknown' must not downgrade a settled verdict
  const spendAttempt = spend(w);
  return {
    pass: s.state === "broken" && s.terminal === true && !spendAttempt.ok,
    detail: `${s.state} terminal=${s.terminal} A=${s.A}`,
  };
});

test(13, "broken refuses a re-seal", (w) => {
  seal(w);
  send(w);
  send(w);
  const s1 = status(w);
  seal(w);                       // terminal latches may be replaced only deliberately
  const spendAttempt = spend(w);
  return { pass: s1.state === "broken" && !spendAttempt.ok, detail: s1.state };
});

const held = cases.filter((c) => c.pass).length;
for (const c of cases) {
  console.log((c.pass ? "HOLD " : "BREAK") + "  " + String(c.id).padStart(2) + "  " + c.title +
              (c.pass ? "" : "   <- " + c.detail));
}
console.log(`GRADE ${held === cases.length ? "A" : "F"}  ${held}/${cases.length}`);
if (held < cases.length) process.exit(1);
