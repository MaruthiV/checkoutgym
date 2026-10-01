import crypto from "node:crypto";
import { Receipt } from "mppx";
import { Mppx, stripe } from "mppx/client";

const token = process.env.MPP_TEST_SPT ?? "";
const url = process.env.MPP_URL ?? "http://127.0.0.1:4242/paid";
if (!token.startsWith("spt_") || process.env.STRIPE_SECRET_KEY || process.env.STRIPE_SK_TEST) {
  throw new Error("buyer needs only an SPT, without a merchant key");
}

let challenges = 0;
let tokenUses = 0;
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

const response = await client.fetch(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: "{}",
});
if (!response.ok) throw new Error(`paid request returned ${response.status}`);

const artifactText = await response.text();
const artifact = JSON.parse(artifactText);
if (artifact.artifact_id !== "fixture-v1" || artifact.data !== "paid-json-v1") {
  throw new Error("unexpected paid artifact");
}
const receipt = Receipt.fromResponse(response);
if (receipt.method !== "stripe" || receipt.status !== "success" || !receipt.reference.startsWith("pi_")) {
  throw new Error("MPP receipt has no PaymentIntent reference");
}
console.log(JSON.stringify({
  response_status: response.status,
  artifact,
  artifact_sha256: crypto.createHash("sha256").update(artifactText).digest("hex"),
  receipt_reference: receipt.reference,
  challenges,
  token_uses: tokenUses,
}));
