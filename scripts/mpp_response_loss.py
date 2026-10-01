"""Reproduce a lost MPP paid response using one capped Stripe test-mode SPT."""

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mpp_baseline import MPP, ROOT, SAMPLE_SHA, STRIPE_VERSION, local_port, redacted, sha256

from checkoutgym.stripe_leg import RealStripe


def run() -> Path:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("STRIPE_SK_TEST", "")
    profile_id = os.environ.get("STRIPE_PROFILE_ID", "")
    if not key.startswith("sk_test_") or not profile_id.startswith("profile_test_"):
        raise RuntimeError("STRIPE_SK_TEST and STRIPE_PROFILE_ID must both be test-mode values")

    stripe = RealStripe(secret_key=key, preview_version=STRIPE_VERSION)
    token = ""
    server = None
    proxy = None
    now = datetime.now(UTC)
    out = ROOT / "results" / f"mpp-response-loss-{now.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=False)
    log = out / "private-artifacts.jsonl"
    fault_log = out / "fault.jsonl"
    try:
        minted = stripe.mint(max_amount=50, expires_in=600)
        if minted.get("livemode") is not False or int(minted.get("usage_limits", {}).get("max_amount", 0)) != 50:
            raise RuntimeError("test SPT did not have the intended $0.50 cap")
        token = minted["id"]
        started = int(time.time()) - 1
        port = local_port()
        server = subprocess.Popen(
            [str(MPP / "node_modules/.bin/tsx"), "main.ts"], cwd=MPP,
            env={
                "PATH": os.environ.get("PATH", ""),
                "STRIPE_SECRET_KEY": key,
                "STRIPE_PROFILE_ID": profile_id,
                "MPP_PORT": str(port),
                "MPP_ARTIFACT_LOG": str(log),
            },
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        url = f"http://127.0.0.1:{port}"
        for _ in range(80):
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                raise RuntimeError(f"server exited: {redacted(stderr, key, token)}")
            try:
                if httpx.get(f"{url}/health", timeout=0.25).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("server did not become healthy")

        proxy_port = local_port()
        proxy = subprocess.Popen(
            [str(MPP / "node_modules/.bin/tsx"), "response_loss_proxy.ts"], cwd=MPP,
            env={
                "PATH": os.environ.get("PATH", ""),
                "MPP_PROXY_PORT": str(proxy_port),
                "MPP_UPSTREAM_PORT": str(port),
                "MPP_FAULT_LOG": str(fault_log),
            },
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        for _ in range(80):
            if proxy.poll() is not None:
                stderr = proxy.stderr.read() if proxy.stderr else ""
                raise RuntimeError(f"proxy exited: {redacted(stderr, key, token)}")
            try:
                if httpx.get(f"{proxy_url}/health", timeout=0.25).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("proxy did not become healthy")

        buyer = subprocess.run(
            [str(MPP / "node_modules/.bin/tsx"), "response_loss_buyer.ts"], cwd=MPP,
            env={
                "PATH": os.environ.get("PATH", ""),
                "MPP_TEST_SPT": token,
                "MPP_URL": f"{proxy_url}/paid",
            },
            capture_output=True, text=True, timeout=60, check=False,
        )
        if buyer.returncode:
            raise RuntimeError(f"buyer exited {buyer.returncode}: {redacted(buyer.stderr, key, token)}")
        result = json.loads(buyer.stdout)
        records = [json.loads(line) for line in log.read_text().splitlines()]
        if len(records) != 1:
            raise RuntimeError(f"expected one durable paid result, got {len(records)}")
        saved = records[0]
        faults = [json.loads(line) for line in fault_log.read_text().splitlines()]
        if len(faults) != 1 or not faults[0]["upstream_receipt_present"]:
            raise RuntimeError("proxy did not drop exactly one completed paid response with receipt")
        fault = faults[0]
        artifact_body = json.dumps(saved["artifact"], separators=(",", ":")).encode()
        if fault["body_sha256"] != sha256(artifact_body):
            raise RuntimeError("dropped response body and durable artifact disagree")
        if saved["request_sha256"] != sha256(b"{}") or saved["route"] != "/paid":
            raise RuntimeError("saved operation does not match the original request")

        reference = saved["payment_intent"]
        response = stripe.http.get(f"/v1/payment_intents/{reference}", headers={"Stripe-Version": STRIPE_VERSION})
        if response.status_code != 200:
            raise RuntimeError(f"PaymentIntent lookup returned {response.status_code}")
        payment = response.json()
        if (payment.get("id") != reference or
                (payment.get("status"), payment.get("amount"), payment.get("currency"), payment.get("livemode")) != ("succeeded", 50, "usd", False) or
                payment.get("metadata", {}).get("machine_payment") != "true"):
            raise RuntimeError("saved result does not correspond to a successful test MPP payment")

        payments_response = stripe.http.get(
            "/v1/payment_intents",
            params={"created[gte]": started, "limit": 100},
            headers={"Stripe-Version": STRIPE_VERSION},
        )
        if payments_response.status_code != 200:
            raise RuntimeError(f"PaymentIntent list returned {payments_response.status_code}")
        payment_list = payments_response.json()
        if payment_list.get("has_more"):
            raise RuntimeError("PaymentIntent list is incomplete")
        observed = [pi for pi in payment_list.get("data", []) if
                    pi.get("metadata", {}).get("machine_payment") == "true" and
                    pi.get("amount") == 50 and pi.get("currency") == "usd"]
        if not any(pi.get("id") == reference for pi in observed):
            raise RuntimeError("saved PaymentIntent absent from run-window list")
        if result["token_uses"] != 1 or result["challenges"] != 1:
            raise RuntimeError("buyer did not begin with exactly one MPP challenge")

        manifest = {
            "timestamp_utc": now.isoformat(),
            "stripe_sample_sha": SAMPLE_SHA,
            "lockfile_sha256": sha256((MPP / "pnpm-lock.yaml").read_bytes()),
            "server_sha256": sha256((MPP / "main.ts").read_bytes()),
            "buyer_sha256": sha256((MPP / "response_loss_buyer.ts").read_bytes()),
            "proxy_sha256": sha256((MPP / "response_loss_proxy.ts").read_bytes()),
            "mppx_version": "0.9.2",
            "stripe_sdk_version": "22.6.2",
            "stripe_api_version": STRIPE_VERSION,
            "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
            "pnpm_version": subprocess.check_output(["pnpm", "--version"], text=True).strip(),
            "stripe_mode": "test",
            "transport": "localhost HTTP proxy with downstream TCP reset before response headers",
        }
        trace = {
            **result,
            "fault": fault,
            "saved_artifact": saved["artifact"],
            "saved_request_sha256": saved["request_sha256"],
            "saved_route": saved["route"],
            "saved_at": saved["saved_at"],
            "saved_payment_intent_sha256": sha256(reference.encode()),
            "stripe_payment_intent_sha256": sha256(str(payment["id"]).encode()),
            "stripe_payment_status": payment["status"],
            "stripe_amount_cents": payment["amount"],
            "stripe_currency": payment["currency"],
            "stripe_livemode": payment["livemode"],
            "stripe_machine_payment_metadata": payment["metadata"]["machine_payment"],
            "run_window_matching_payment_intents": len(observed),
            "buyer_received_merchant_key": False,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (out / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        return out
    finally:
        if proxy is not None:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
            if proxy.stderr:
                proxy.stderr.close()
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            if server.stderr:
                server.stderr.close()
        stripe.http.close()


if __name__ == "__main__":
    print(run())
