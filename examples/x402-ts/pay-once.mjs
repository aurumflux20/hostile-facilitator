// One purchase through the official x402 TypeScript SDK, for `hostile-facilitator test`.
//
// Seller: Express + @x402/express payment middleware, facilitator = $FACILITATOR_URL.
// Buyer:  @x402/fetch with a throwaway key (no funds; nothing touches a chain).
//
// The seller's facilitator timeout is set from $HOSTILE_CLIENT_TIMEOUT, so the
// timeout modes really time out. Everything else is the SDK's default behaviour.
//
//   npm install
//   hostile-facilitator test --client-timeout 3 -- node pay-once.mjs
import express from "express";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import { HTTPFacilitatorClient } from "@x402/core/server";
import { ExactEvmScheme as ExactEvmServer } from "@x402/evm/exact/server";
import { ExactEvmScheme } from "@x402/evm";
import { wrapFetchWithPaymentFromConfig } from "@x402/fetch";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

const NETWORK = "eip155:84532";
const PAY_TO = "0x000000000000000000000000000000000000dEaD";
const timeoutMs = Math.round(1000 * Number(process.env.HOSTILE_CLIENT_TIMEOUT ?? "5"));

const facilitator = new HTTPFacilitatorClient({ url: process.env.FACILITATOR_URL, timeoutMs });
const resourceServer = new x402ResourceServer(facilitator).register(NETWORK, new ExactEvmServer());

const app = express();
app.use(paymentMiddleware(
  { "GET /paid": { accepts: [{ scheme: "exact", price: "$0.01", network: NETWORK, payTo: PAY_TO }] } },
  resourceServer,
));
app.get("/paid", (_req, res) => res.json({ ok: true }));

const server = app.listen(0, "127.0.0.1", async () => {
  const { port } = server.address();
  const pay = wrapFetchWithPaymentFromConfig(fetch, {
    schemes: [{ network: NETWORK, client: new ExactEvmScheme(privateKeyToAccount(generatePrivateKey())) }],
  });
  let code = 1;
  try {
    const r = await pay(`http://127.0.0.1:${port}/paid`, { method: "GET" });
    console.log(`buyer got HTTP ${r.status}`);
    code = r.status === 200 ? 0 : 1;
  } catch (e) {
    console.log(`buyer error: ${e?.message ?? e}`);
  } finally {
    server.close();
    process.exit(code);
  }
});
