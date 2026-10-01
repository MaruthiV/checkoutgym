import crypto from "node:crypto";
import { closeSync, fsyncSync, openSync, readFileSync, writeSync } from "node:fs";
import { Receipt } from "mppx";
import { Mppx, stripe } from "mppx/client";

const mode = process.argv[2];
const base = process.env.MPP_URL ?? "";
const statePath = process.env.MPP_CLIENT_STATE ?? "";
const buyerKey = process.env.MPP_BUYER_KEY ?? "";
if (!base || !statePath || buyerKey.length < 32 ||
    process.env.STRIPE_SECRET_KEY || process.env.STRIPE_SK_TEST) {
  throw new Error("buyer requires client keys/state but no merchant key");
}

type State = { operationId: string; body: string };
type Artifact = { artifact_id: string; data: string; target: string; screenshot_png_base64: string; screenshot_sha256: string };
const headers = (key: string) => ({ "Content-Type": "application/json", "X-Buyer-Key": key });
const pngMagic = Buffer.from("89504e470d0a1a0a", "hex");

function inspectArtifact(artifact: Artifact, operationId: string) {
  if (artifact.artifact_id !== `op-${operationId}` || artifact.data !== "paid-result-for-alpha" ||
      artifact.target !== "alpha" || typeof artifact.screenshot_png_base64 !== "string" ||
      typeof artifact.screenshot_sha256 !== "string") {
    throw new Error("artifact does not match the original operation");
  }
  const png = Buffer.from(artifact.screenshot_png_base64, "base64");
  const actualSha = crypto.createHash("sha256").update(png).digest("hex");
  if (!png.subarray(0, 8).equals(pngMagic) || actualSha !== artifact.screenshot_sha256) {
    throw new Error("paid screenshot is not a valid matching PNG");
  }
  return {
    png,
    descriptor: {
      artifact_id: artifact.artifact_id,
      data: artifact.data,
      target: artifact.target,
      screenshot_sha256: artifact.screenshot_sha256,
    },
  };
}

async function start() {
  const token = process.env.MPP_TEST_SPT ?? "";
  if (!token.startsWith("spt_")) throw new Error("start phase requires a test SPT");
  const body = '{"target":"alpha"}';
  const created = await fetch(`${base}/operations`, {
    method: "POST", headers: headers(buyerKey), body,
  });
  if (created.status !== 201) throw new Error(`operation creation returned ${created.status}`);
  const result = await created.json() as { operation_id: string; price_cents: number; currency: string };
  if (result.price_cents !== 50 || result.currency !== "usd" || !/^[a-f0-9-]+$/.test(result.operation_id)) {
    throw new Error("unexpected operation offer");
  }
  const state: State = { operationId: result.operation_id, body };
  const fd = openSync(statePath, "w", 0o600);
  try {
    writeSync(fd, JSON.stringify(state) + "\n");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }

  let tokenUses = 0;
  let challenges = 0;
  let credential = "";
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
  let clientError = "";
  const payUrl = `${base}/operations/${state.operationId}/pay`;
  try {
    const response = await client.fetch(payUrl, {
      method: "POST", headers: headers(buyerKey), body,
    });
    throw new Error(`fault did not interrupt the purchase: ${response.status}`);
  } catch (error) {
    if (error instanceof TypeError && error.message === "fetch failed") {
      clientError = `${error.name}: ${error.message}`;
    } else {
      throw error;
    }
  }
  if (!credential || tokenUses !== 1 || challenges !== 1) throw new Error("unexpected payment flow");
  const replayInit = client.transport.setCredential({ method: "POST", headers: headers(buyerKey), body }, credential);
  const replay = await client.rawFetch(payUrl, replayInit);
  console.log(JSON.stringify({
    operation_id: state.operationId,
    request_sha256: crypto.createHash("sha256").update(body).digest("hex"),
    client_error: clientError,
    challenges,
    token_uses: tokenUses,
    replay_status: replay.status,
    replay_received_artifact: (await replay.text()).includes("paid-result-for-alpha"),
  }));
}

async function recover() {
  if (process.env.MPP_TEST_SPT) throw new Error("recovery phase must not have an SPT");
  const state = JSON.parse(readFileSync(statePath, "utf8")) as State;
  const url = `${base}/operations/${state.operationId}/result`;
  const lookup = (key: string, body: string, path = url) => fetch(path, {
    method: "POST", headers: headers(key), body,
  });
  const changedBody = await lookup(buyerKey, '{"target":"beta"}');
  const wrongRoute = await lookup(buyerKey, state.body, `${url}/other`);
  const response = await lookup(buyerKey, state.body);
  if (response.status !== 200) throw new Error(`recovery returned ${response.status}`);
  const artifactText = await response.text();
  const artifact = JSON.parse(artifactText) as Artifact;
  const inspected = inspectArtifact(artifact, state.operationId);
  const output = process.env.MPP_RECOVERED_PNG ?? "";
  if (!output) throw new Error("recovery requires MPP_RECOVERED_PNG");
  const fd = openSync(output, "w", 0o600);
  try {
    writeSync(fd, inspected.png);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  const receipt = Receipt.fromResponse(response);
  if (receipt.method !== "stripe" || receipt.status !== "success" || !receipt.reference.startsWith("pi_")) {
    throw new Error("recovered result has no valid Stripe MPP receipt");
  }
  const repeated = await lookup(buyerKey, state.body);
  const repeatedText = await repeated.text();
  console.log(JSON.stringify({
    operation_id: state.operationId,
    changed_body_status: changedBody.status,
    wrong_route_status: wrongRoute.status,
    recovery_status: response.status,
    artifact: inspected.descriptor,
    artifact_sha256: crypto.createHash("sha256").update(artifactText).digest("hex"),
    receipt_reference: receipt.reference,
    repeated_recovery_status: repeated.status,
    repeated_same_artifact: repeatedText === artifactText,
  }));
}

async function intruder() {
  if (process.env.MPP_TEST_SPT) throw new Error("intruder phase must not have an SPT");
  const state = JSON.parse(readFileSync(statePath, "utf8")) as State;
  const response = await fetch(`${base}/operations/${state.operationId}/result`, {
    method: "POST", headers: headers(buyerKey), body: state.body,
  });
  const text = await response.text();
  console.log(JSON.stringify({
    wrong_buyer_status: response.status,
    wrong_buyer_received_artifact: text.includes("paid-result-for-alpha"),
    wrong_buyer_received_receipt: response.headers.has("Payment-Receipt"),
  }));
}

async function secondPurchase() {
  const token = process.env.MPP_TEST_SPT ?? "";
  if (!token.startsWith("spt_")) throw new Error("second purchase requires its own test SPT");
  const original = JSON.parse(readFileSync(statePath, "utf8")) as State;
  const created = await fetch(`${base}/operations`, {
    method: "POST", headers: headers(buyerKey), body: original.body,
  });
  if (created.status !== 201) throw new Error(`second operation creation returned ${created.status}`);
  const offer = await created.json() as { operation_id: string; price_cents: number; currency: string };
  if (offer.operation_id === original.operationId || offer.price_cents !== 50 || offer.currency !== "usd") {
    throw new Error("second offer did not create a distinct operation at the same price");
  }
  let tokenUses = 0;
  const client = Mppx.create({
    methods: [stripe({
      paymentMethod: "pm_card_visa",
      createToken: async ({ amount, currency, networkId }) => {
        if (amount !== "50" || currency !== "usd" || !networkId?.startsWith("profile_test_")) {
          throw new Error("unexpected second payment challenge");
        }
        tokenUses += 1;
        return token;
      },
    })],
    polyfill: false,
  });
  const paid = await client.fetch(`${base}/operations/${offer.operation_id}/pay`, {
    method: "POST", headers: headers(buyerKey), body: original.body,
  });
  if (paid.status !== 200 || tokenUses !== 1) throw new Error(`second purchase returned ${paid.status}`);
  const artifactText = await paid.text();
  const artifact = JSON.parse(artifactText) as Artifact;
  const inspected = inspectArtifact(artifact, offer.operation_id);
  const receipt = Receipt.fromResponse(paid);
  if (receipt.status !== "success" ||
      !receipt.reference.startsWith("pi_")) {
    throw new Error("second purchase artifact or receipt mismatch");
  }
  console.log(JSON.stringify({
    operation_id: offer.operation_id,
    payment_intent: receipt.reference,
    artifact: inspected.descriptor,
    artifact_sha256: crypto.createHash("sha256").update(artifactText).digest("hex"),
    token_uses: tokenUses,
  }));
}

if (mode === "start") await start();
else if (mode === "recover") await recover();
else if (mode === "intruder") await intruder();
else if (mode === "second") await secondPurchase();
else throw new Error("expected start, recover, intruder, or second phase");
