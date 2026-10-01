import crypto from "node:crypto";
import { closeSync, existsSync, fsyncSync, openSync, readFileSync, writeSync } from "node:fs";
import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { Receipt } from "mppx";
import { Mppx, stripe } from "mppx/server";
import StripeClient from "stripe";
import { screenshot } from "./screenshot.js";

const key = process.env.STRIPE_SECRET_KEY ?? "";
const profileId = process.env.STRIPE_PROFILE_ID ?? "";
const buyerAKey = process.env.MPP_BUYER_A_KEY ?? "";
const buyerBKey = process.env.MPP_BUYER_B_KEY ?? "";
const logPath = process.env.MPP_RECOVERY_LOG ?? "";
if (!key.startsWith("sk_test_") || !profileId.startsWith("profile_test_") ||
    buyerAKey.length < 32 || buyerBKey.length < 32 || !logPath) {
  throw new Error("test-mode Stripe setup, two buyer keys, and a recovery log are required");
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

type Operation = {
  id: string;
  buyer: "a" | "b";
  requestSha256: string;
  target: string;
  priceCents: 50;
  currency: "usd";
  createdAt: string;
  status: "created" | "payment_unknown" | "ready";
  artifact?: { artifact_id: string; data: string; target: string; screenshot_png_base64: string; screenshot_sha256: string };
  paymentIntent?: string;
  receiptHeader?: string;
};
const operations = new Map<string, Operation>();
if (existsSync(logPath)) {
  for (const line of readFileSync(logPath, "utf8").split("\n")) {
    if (!line) continue;
    const operation = JSON.parse(line) as Operation;
    operations.set(operation.id, operation);
  }
}

function save(operation: Operation): void {
  const fd = openSync(logPath, "a", 0o600);
  try {
    writeSync(fd, JSON.stringify(operation) + "\n");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  operations.set(operation.id, operation);
}

function sameKey(provided: string, expected: string): boolean {
  const a = crypto.createHash("sha256").update(provided).digest();
  const b = crypto.createHash("sha256").update(expected).digest();
  return crypto.timingSafeEqual(a, b);
}

function buyer(request: Request): "a" | "b" | null {
  const provided = request.headers.get("X-Buyer-Key") ?? "";
  if (sameKey(provided, buyerAKey)) return "a";
  if (sameKey(provided, buyerBKey)) return "b";
  return null;
}

async function bodyInfo(request: Request): Promise<{ sha256: string; target: string } | null> {
  const body = await request.clone().text();
  if (body.length > 2048) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || !("target" in parsed) || parsed.target !== "alpha") return null;
  return { sha256: crypto.createHash("sha256").update(body).digest("hex"), target: parsed.target };
}

app.get("/health", (c) => c.json({ ok: true }));
app.post("/operations", async (c) => {
  const principal = buyer(c.req.raw);
  if (!principal) return c.json({ error: "unauthorized" }, 401);
  const input = await bodyInfo(c.req.raw);
  if (!input) return c.json({ error: "invalid_input" }, 400);
  const operation: Operation = {
    id: crypto.randomUUID(),
    buyer: principal,
    requestSha256: input.sha256,
    target: input.target,
    priceCents: 50,
    currency: "usd",
    createdAt: new Date().toISOString(),
    status: "created",
  };
  save(operation);
  return c.json({ operation_id: operation.id, price_cents: 50, currency: "usd" }, 201);
});

app.post("/operations/:id/pay", async (c) => {
  const principal = buyer(c.req.raw);
  if (!principal) return c.json({ error: "unauthorized" }, 401);
  const operation = operations.get(c.req.param("id"));
  if (!operation || operation.buyer !== principal) return c.json({ error: "not_found" }, 404);
  if (operation.priceCents !== 50 || operation.currency !== "usd") return c.json({ error: "price_mismatch" }, 409);
  const input = await bodyInfo(c.req.raw);
  if (!input || input.sha256 !== operation.requestSha256) return c.json({ error: "request_mismatch" }, 409);
  if (operation.status === "ready") return c.json({ error: "already_paid_use_result" }, 409);
  if (operation.status === "payment_unknown") return c.json({ status: "payment_unknown" }, 202);

  if (c.req.header("Authorization")?.startsWith("Payment ")) {
    // This is synced before Stripe verification. A crash in the next window
    // remains explicitly unknown, so a new credential cannot blindly repay it.
    operation.status = "payment_unknown";
    save(operation);
  }
  const response = await paid(c.req.raw);
  if (response.status === 402) return response.challenge;

  const image = await screenshot();
  const artifact = {
    artifact_id: `op-${operation.id}`,
    data: `paid-result-for-${operation.target}`,
    target: operation.target,
    screenshot_png_base64: image.pngBase64,
    screenshot_sha256: image.sha256,
  };
  const delivered = response.withReceipt(Response.json(artifact));
  const receipt = Receipt.fromResponse(delivered);
  const receiptHeader = delivered.headers.get("Payment-Receipt");
  if (!receiptHeader) throw new Error("successful MPP response has no receipt header");
  operation.status = "ready";
  operation.artifact = artifact;
  operation.paymentIntent = receipt.reference;
  operation.receiptHeader = receiptHeader;
  save(operation);
  return delivered;
});

app.post("/operations/:id/result", async (c) => {
  const principal = buyer(c.req.raw);
  if (!principal) return c.json({ error: "unauthorized" }, 401);
  const operation = operations.get(c.req.param("id"));
  if (!operation || operation.buyer !== principal) return c.json({ error: "not_found" }, 404);
  const input = await bodyInfo(c.req.raw);
  if (!input || input.sha256 !== operation.requestSha256) return c.json({ error: "request_mismatch" }, 409);
  if (operation.status !== "ready" || !operation.artifact || !operation.receiptHeader) {
    return c.json({ status: operation.status }, 202);
  }
  return new Response(JSON.stringify(operation.artifact), {
    status: 200,
    headers: { "Content-Type": "application/json", "Payment-Receipt": operation.receiptHeader },
  });
});

const port = Number(process.env.MPP_PORT ?? "4242");
serve({ fetch: app.fetch, hostname: "127.0.0.1", port });
