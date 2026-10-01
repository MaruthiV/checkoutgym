"""Check that an unresolved operation stays pending without touching Stripe."""

import json
import os
import secrets
import subprocess
import time
import uuid
from datetime import UTC, datetime

import httpx
from mpp_baseline import MPP, ROOT, local_port, sha256


def run():
    now = datetime.now(UTC)
    out = ROOT / "results" / f"mpp-unknown-control-{now.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=False)
    log = out / "private-operations.jsonl"
    operation_id = str(uuid.uuid4())
    body = '{"target":"alpha"}'
    buyer_a_key = secrets.token_hex(32)
    buyer_b_key = secrets.token_hex(32)
    operation = {
        "id": operation_id,
        "buyer": "a",
        "requestSha256": sha256(body.encode()),
        "target": "alpha",
        "priceCents": 50,
        "currency": "usd",
        "createdAt": now.isoformat(),
        "status": "payment_unknown",
    }
    log.write_text(json.dumps(operation) + "\n")
    port = local_port()
    server = subprocess.Popen(
        [str(MPP / "node_modules/.bin/tsx"), "recovery_server.ts"], cwd=MPP,
        env={
            "PATH": os.environ.get("PATH", ""),
            "STRIPE_SECRET_KEY": "sk_test_fixture",
            "STRIPE_PROFILE_ID": "profile_test_fixture",
            "MPP_PORT": str(port),
            "MPP_RECOVERY_LOG": str(log),
            "MPP_BUYER_A_KEY": buyer_a_key,
            "MPP_BUYER_B_KEY": buyer_b_key,
        },
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}/operations/{operation_id}"
        for _ in range(80):
            if server.poll() is not None:
                raise RuntimeError(f"server exited: {server.stderr.read()[:500] if server.stderr else ''}")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.25).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("server did not become healthy")
        headers_a = {"Content-Type": "application/json", "X-Buyer-Key": buyer_a_key}
        headers_b = {"Content-Type": "application/json", "X-Buyer-Key": buyer_b_key}
        result = httpx.post(f"{base}/result", headers=headers_a, content=body, timeout=5)
        retry_pay = httpx.post(f"{base}/pay", headers=headers_a, content=body, timeout=5)
        intruder = httpx.post(f"{base}/result", headers=headers_b, content=body, timeout=5)
        if (result.status_code, retry_pay.status_code, intruder.status_code) != (202, 202, 404):
            raise RuntimeError("unknown payment was repaid or revealed")
        if result.json() != {"status": "payment_unknown"} or retry_pay.json() != {"status": "payment_unknown"}:
            raise RuntimeError("unknown state was not reported accurately")
        trace = {
            "setup": "synthetic local state fixture; no Stripe API call or payment",
            "operation_id_sha256": sha256(operation_id.encode()),
            "status_before": "payment_unknown",
            "original_buyer_result_status": result.status_code,
            "original_buyer_retry_pay_status": retry_pay.status_code,
            "different_buyer_result_status": intruder.status_code,
            "stripe_calls": 0,
        }
        (out / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        return out
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        if server.stderr:
            server.stderr.close()


if __name__ == "__main__":
    print(run())
