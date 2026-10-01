import crypto from "node:crypto";
import { Mppx, stripe } from "mppx/client";

const token = process.env.MPP_TEST_SPT ?? "";
const url = process.env.MPP_URL ?? "http://127.0.0.1:4242/paid";
if (!token.startsWith("spt_") || process.env.STRIPE_SECRET_KEY || process.env.STRIPE_SK_TEST) {
  throw new Error("buyer needs only an SPT, without a merchant key");
}

let credential = "";
let tokenUses = 0;
let challenges = 0;

const client = Mppx.create({
  methods: [stripe({
    paymentMethod: "pm_card_visa",
    createToken: async ({ amount, currency, networkId }) => {
      if (amount !== "50" || currency !== "usd" || !networkId?.startsWith("profile_test_")) {
        throw new Error("unexpected payment challenge");
      }
      tokenUses += 1;
      return token;
    },
  })],
  polyfill: false,
});
client.onChallengeReceived(() => { challenges += 1; });
client.onCredentialCreated((event) => { credential = event.credential; });

const initial = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
let clientError = "";
try {
  const response = await client.fetch(url, initial);
  throw new Error(`fault did not interrupt the purchase: ${response.status}`);
} catch (error) {
  if (error instanceof TypeError && error.message === "fetch failed") {
    clientError = `${error.name}: ${error.message}`;
  } else {
    throw error;
  }
}
if (!credential) throw new Error("the paid request did not fail after credential creation");

async function retry(path: string, body: string) {
  const request = client.transport.setCredential({ ...initial, body }, credential);
  const response = await client.rawFetch(new URL(path, url), request);
  const text = await response.text();
  return {
    status: response.status,
    received_artifact: text.includes("paid-json-v1"),
    received_receipt: response.headers.has("Payment-Receipt"),
  };
}

const same = await retry("/paid", "{}");
const repeated = await retry("/paid", "{}");
const changedBody = await retry("/paid", '{"query":"changed"}');
const changedRoute = await retry("/paid/alternate", "{}");
console.log(JSON.stringify({
  client_error: clientError,
  credential_sha256: crypto.createHash("sha256").update(credential).digest("hex"),
  token_uses: tokenUses,
  challenges,
  same_credential_retry: same,
  repeated_credential_retry: repeated,
  changed_body_retry: changedBody,
  changed_route_retry: changedRoute,
}));
