import hashlib
import json
import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from checkoutgym.stripe_leg import RealStripe

ROOT = Path(__file__).resolve().parents[1]
MPP = ROOT / "mpp"
SAMPLE_SHA = "ee4b2cf9e1d4a2a1c5670b266a4cb97a3d9420f8"
STRIPE_VERSION = "2026-07-29.preview"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def redacted(text: str, key: str, token: str) -> str:
    return text.replace(key, "<stripe-key>").replace(token, "<spt>")[:1200]


def run() -> Path:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("STRIPE_SK_TEST", "")
    profile_id = os.environ.get("STRIPE_PROFILE_ID", "")
    if not key.startswith("sk_test_") or not profile_id.startswith("profile_test_"):
        raise RuntimeError("STRIPE_SK_TEST and STRIPE_PROFILE_ID must both be test-mode values")

    stripe = RealStripe(secret_key=key, preview_version=STRIPE_VERSION)
    token = ""
    server = None
    try:
        minted = stripe.mint(max_amount=50, expires_in=600)
        if minted.get("livemode") is not False or int(minted.get("usage_limits", {}).get("max_amount", 0)) != 50:
            raise RuntimeError("test SPT did not have the intended $0.50 cap")
        token = minted["id"]
        port = local_port()
        server_env = {
            "PATH": os.environ.get("PATH", ""),
            "STRIPE_SECRET_KEY": key,
            "STRIPE_PROFILE_ID": profile_id,
            "MPP_PORT": str(port),
        }
        server = subprocess.Popen(
            [str(MPP / "node_modules/.bin/tsx"), "main.ts"], cwd=MPP,
            env=server_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
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

        buyer_env = {
            "PATH": os.environ.get("PATH", ""),
            "MPP_TEST_SPT": token,
            "MPP_URL": f"{url}/paid",
        }
        buyer = subprocess.run(
            [str(MPP / "node_modules/.bin/tsx"), "buyer.ts"], cwd=MPP,
            env=buyer_env, capture_output=True, text=True, timeout=60, check=False,
        )
        if buyer.returncode:
            raise RuntimeError(f"buyer exited {buyer.returncode}: {redacted(buyer.stderr, key, token)}")
        result = json.loads(buyer.stdout)
        reference = result["receipt_reference"]
        response = stripe.http.get(f"/v1/payment_intents/{reference}", headers={"Stripe-Version": STRIPE_VERSION})
        if response.status_code != 200:
            raise RuntimeError(f"PaymentIntent lookup returned {response.status_code}")
        payment = response.json()
        if (payment.get("id") != reference or
                (payment.get("status"), payment.get("amount"), payment.get("currency"), payment.get("livemode")) != ("succeeded", 50, "usd", False)):
            raise RuntimeError("Stripe PaymentIntent did not match the test purchase")
        if payment.get("metadata", {}).get("machine_payment") != "true":
            raise RuntimeError("Stripe PaymentIntent lacks MPP metadata")
        if result["response_status"] != 200 or result["challenges"] != 1 or result["token_uses"] != 1:
            raise RuntimeError("buyer did not complete exactly one MPP payment challenge")

        now = datetime.now(UTC)
        out = ROOT / "results" / f"mpp-baseline-{now.strftime('%Y%m%d-%H%M%S')}"
        out.mkdir(parents=True, exist_ok=False)
        manifest = {
            "timestamp_utc": now.isoformat(),
            "stripe_sample_sha": SAMPLE_SHA,
            "lockfile_sha256": sha256((MPP / "pnpm-lock.yaml").read_bytes()),
            "server_sha256": sha256((MPP / "main.ts").read_bytes()),
            "buyer_sha256": sha256((MPP / "buyer.ts").read_bytes()),
            "mppx_version": "0.9.2",
            "stripe_sdk_version": "22.6.2",
            "stripe_api_version": STRIPE_VERSION,
            "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
            "pnpm_version": subprocess.check_output(["pnpm", "--version"], text=True).strip(),
            "stripe_mode": "test",
            "transport": "localhost HTTP",
        }
        trace = {
            "challenge_count": result["challenges"],
            "token_factory_calls": result["token_uses"],
            "client_status": result["response_status"],
            "artifact": result["artifact"],
            "artifact_sha256": result["artifact_sha256"],
            "receipt_payment_intent_sha256": sha256(reference.encode()),
            "stripe_payment_intent_sha256": sha256(str(payment["id"]).encode()),
            "stripe_payment_status": payment["status"],
            "stripe_amount_cents": payment["amount"],
            "stripe_currency": payment["currency"],
            "stripe_livemode": payment["livemode"],
            "stripe_machine_payment_metadata": payment["metadata"]["machine_payment"],
            "buyer_received_merchant_key": False,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (out / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        return out
    finally:
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
