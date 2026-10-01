import crypto from "node:crypto";
import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { discovery } from "mppx/hono";
import { Mppx, stripe } from "mppx/server";
import StripeClient from "stripe";

const key = process.env.STRIPE_SECRET_KEY ?? "";
const profileId = process.env.STRIPE_PROFILE_ID ?? "";
if (!key.startsWith("sk_test_") || !profileId.startsWith("profile_test_")) {
  throw new Error("test-mode Stripe key and profile are required");
}

const secretKey = crypto.createHmac("sha256", key).update("mpp-challenge-signing").digest("base64");
const stripeClient = new StripeClient(key);
const machinePayments = stripe.create({ client: stripeClient, networkId: profileId, livemode: false });
const mppx = Mppx.create({
  methods: machinePayments.defaultMethods({ exclude: ["tempo"] }),
  secretKey,
});
const paid = mppx.charge({ amount: "0.50" });
const app = new Hono();

app.get("/health", (c) => c.json({ ok: true }));
app.post("/paid", async (c) => {
  const response = await paid(c.req.raw);
  if (response.status === 402) return response.challenge;
  return response.withReceipt(Response.json({ artifact_id: "fixture-v1", data: "paid-json-v1" }));
});

discovery(app, mppx, {
  info: { title: "CheckoutGym MPP baseline", version: "1.0.0" },
  routes: [{
    handler: paid,
    method: "POST",
    path: "/paid",
    requestBody: {
      content: { "application/json": { schema: { type: "object" } } },
      description: "Optional JSON request data.",
      required: false,
    },
    summary: "Returns one paid JSON artifact",
  }],
});

const port = Number(process.env.MPP_PORT ?? "4242");
serve({ fetch: app.fetch, hostname: "127.0.0.1", port });
console.log(`MPP baseline listening on 127.0.0.1:${port}`);

export { app };
