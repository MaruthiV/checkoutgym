"""Verify lost-response recovery for one authenticated MPP operation in test mode."""

import json
import os
import secrets
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mpp_baseline import MPP, ROOT, SAMPLE_SHA, STRIPE_VERSION, local_port, redacted, sha256

from checkoutgym.stripe_leg import RealStripe


def wait_healthy(process: subprocess.Popen, url: str, key: str, token: str, label: str) -> None:
    for _ in range(80):
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"{label} exited: {redacted(stderr, key, token)}")
        try:
            if httpx.get(f"{url}/health", timeout=0.25).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{label} did not become healthy")


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.stderr:
        process.stderr.close()


def run() -> Path:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("STRIPE_SK_TEST", "")
    profile_id = os.environ.get("STRIPE_PROFILE_ID", "")
    if not key.startswith("sk_test_") or not profile_id.startswith("profile_test_"):
        raise RuntimeError("STRIPE_SK_TEST and STRIPE_PROFILE_ID must both be test-mode values")

    stripe = RealStripe(secret_key=key, preview_version=STRIPE_VERSION)
    token = ""
    second_token = ""
    server = None
    proxy = None
    now = datetime.now(UTC)
    out = ROOT / "results" / f"mpp-recovery-{now.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=False)
    log = out / "private-operations.jsonl"
    fault_log = out / "fault.jsonl"
    client_state = out / "client-state.json"
    buyer_a_key = secrets.token_hex(32)
    buyer_b_key = secrets.token_hex(32)
    chrome = (os.environ.get("MPP_CHROME_BIN") or shutil.which("google-chrome") or
              shutil.which("chromium") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not Path(chrome).is_file():
        raise RuntimeError("set MPP_CHROME_BIN to a local Chrome/Chromium executable")
    recovered_png = out / "recovered-screenshot.png"
    try:
        minted = stripe.mint(max_amount=50, expires_in=600)
        if minted.get("livemode") is not False or int(minted.get("usage_limits", {}).get("max_amount", 0)) != 50:
            raise RuntimeError("test SPT did not have the intended $0.50 cap")
        token = minted["id"]
        started = int(time.time()) - 1
        port = local_port()
        server_env = {
            "PATH": os.environ.get("PATH", ""),
            "STRIPE_SECRET_KEY": key,
            "STRIPE_PROFILE_ID": profile_id,
            "MPP_PORT": str(port),
            "MPP_RECOVERY_LOG": str(log),
            "MPP_BUYER_A_KEY": buyer_a_key,
            "MPP_BUYER_B_KEY": buyer_b_key,
            "MPP_CHROME_BIN": chrome,
        }

        def launch_server() -> subprocess.Popen:
            process = subprocess.Popen(
                [str(MPP / "node_modules/.bin/tsx"), "recovery_server.ts"], cwd=MPP,
                env=server_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            wait_healthy(process, f"http://127.0.0.1:{port}", key, token, "recovery server")
            return process

        server = launch_server()
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
        buyer_url = f"http://127.0.0.1:{proxy_port}"
        wait_healthy(proxy, buyer_url, key, token, "fault proxy")
        buyer_env = {
            "PATH": os.environ.get("PATH", ""),
            "MPP_URL": buyer_url,
            "MPP_CLIENT_STATE": str(client_state),
        }
        started_buyer = subprocess.run(
            [str(MPP / "node_modules/.bin/tsx"), "recovery_buyer.ts", "start"], cwd=MPP,
            env={**buyer_env, "MPP_BUYER_KEY": buyer_a_key, "MPP_TEST_SPT": token},
            capture_output=True, text=True, timeout=60, check=False,
        )
        if started_buyer.returncode:
            raise RuntimeError(f"start buyer exited {started_buyer.returncode}: {redacted(started_buyer.stderr, key, token)}")
        start_result = json.loads(started_buyer.stdout)

        # Simulate a buyer/server process restart before recovery. The proxy and
        # persisted operation/client files survive; the recovery process has no SPT.
        stop(server)
        server = launch_server()
        recovered_buyer = subprocess.run(
            [str(MPP / "node_modules/.bin/tsx"), "recovery_buyer.ts", "recover"], cwd=MPP,
            env={**buyer_env, "MPP_BUYER_KEY": buyer_a_key, "MPP_RECOVERED_PNG": str(recovered_png)},
            capture_output=True, text=True, timeout=30, check=False,
        )
        if recovered_buyer.returncode:
            raise RuntimeError(f"recover buyer exited {recovered_buyer.returncode}: {redacted(recovered_buyer.stderr, key, token)}")
        recovered = json.loads(recovered_buyer.stdout)
        intruder_buyer = subprocess.run(
            [str(MPP / "node_modules/.bin/tsx"), "recovery_buyer.ts", "intruder"], cwd=MPP,
            env={**buyer_env, "MPP_BUYER_KEY": buyer_b_key},
            capture_output=True, text=True, timeout=30, check=False,
        )
        if intruder_buyer.returncode:
            raise RuntimeError(f"intruder buyer exited {intruder_buyer.returncode}: {redacted(intruder_buyer.stderr, key, token)}")
        intruder = json.loads(intruder_buyer.stdout)

        records = [json.loads(line) for line in log.read_text().splitlines()]
        faults = [json.loads(line) for line in fault_log.read_text().splitlines()]
        if ([r["status"] for r in records] != ["created", "payment_unknown", "ready"] or
                len(faults) != 1 or not faults[0]["upstream_receipt_present"]):
            raise RuntimeError("operation transition or whole-response fault did not occur exactly once")
        saved = records[-1]
        operation_id = saved["id"]
        if (operation_id != start_result["operation_id"] or operation_id != recovered["operation_id"] or
                saved["buyer"] != "a" or saved["requestSha256"] != start_result["request_sha256"] or
                (saved["priceCents"], saved["currency"]) != (50, "usd")):
            raise RuntimeError("saved operation identity does not match the client")
        saved_artifact = saved["artifact"]
        saved_descriptor = {k: v for k, v in saved_artifact.items() if k != "screenshot_png_base64"}
        if (saved_descriptor != recovered["artifact"] or
                faults[0]["body_sha256"] != recovered["artifact_sha256"]):
            raise RuntimeError("recovered artifact does not match the paid response")
        png = recovered_png.read_bytes()
        if sha256(png) != saved_artifact["screenshot_sha256"] or len(png) < 1000:
            raise RuntimeError("recovered screenshot file does not match the saved PNG")
        reference = saved["paymentIntent"]
        if reference != recovered["receipt_reference"]:
            raise RuntimeError("recovered receipt does not match the paid PaymentIntent")
        if (start_result["client_error"] != "TypeError: fetch failed" or
                (start_result["challenges"], start_result["token_uses"]) != (1, 1) or
                start_result["replay_status"] != 409 or start_result["replay_received_artifact"] or
                intruder["wrong_buyer_status"] != 404 or intruder["wrong_buyer_received_artifact"] or
                intruder["wrong_buyer_received_receipt"] or recovered["changed_body_status"] != 409 or
                recovered["wrong_route_status"] != 404 or recovered["recovery_status"] != 200 or
                recovered["repeated_recovery_status"] != 200 or not recovered["repeated_same_artifact"]):
            raise RuntimeError("client recovery or ownership controls failed")

        response = stripe.http.get(f"/v1/payment_intents/{reference}", headers={"Stripe-Version": STRIPE_VERSION})
        if response.status_code != 200:
            raise RuntimeError(f"PaymentIntent lookup returned {response.status_code}")
        payment = response.json()
        if (payment.get("id") != reference or
                (payment.get("status"), payment.get("amount"), payment.get("currency"), payment.get("livemode")) != ("succeeded", 50, "usd", False) or
                payment.get("metadata", {}).get("machine_payment") != "true"):
            raise RuntimeError("saved operation does not correspond to a successful test MPP payment")
        payments_response = stripe.http.get(
            "/v1/payment_intents", params={"created[gte]": started, "limit": 100},
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
        if len(observed) != 1 or observed[0].get("id") != reference:
            raise RuntimeError("run window did not contain exactly one matching MPP PaymentIntent")

        # A fresh operation with identical request bytes must remain a distinct,
        # intentionally authorized purchase rather than silently reusing result 1.
        second_minted = stripe.mint(max_amount=50, expires_in=600)
        if (second_minted.get("livemode") is not False or
                int(second_minted.get("usage_limits", {}).get("max_amount", 0)) != 50):
            raise RuntimeError("second test SPT did not have the intended $0.50 cap")
        second_token = second_minted["id"]
        second_buyer = subprocess.run(
            [str(MPP / "node_modules/.bin/tsx"), "recovery_buyer.ts", "second"], cwd=MPP,
            env={**buyer_env, "MPP_BUYER_KEY": buyer_a_key, "MPP_TEST_SPT": second_token},
            capture_output=True, text=True, timeout=60, check=False,
        )
        if second_buyer.returncode:
            message = redacted(second_buyer.stderr, key, token).replace(second_token, "<spt>")
            raise RuntimeError(f"second buyer exited {second_buyer.returncode}: {message}")
        second_result = json.loads(second_buyer.stdout)
        after = [json.loads(line) for line in log.read_text().splitlines()]
        if (len(after) != 6 or [r["status"] for r in after[3:]] != ["created", "payment_unknown", "ready"] or
                second_result["operation_id"] == operation_id or second_result["token_uses"] != 1 or
                after[-1]["id"] != second_result["operation_id"] or
                after[-1]["paymentIntent"] != second_result["payment_intent"] or
                {k: v for k, v in after[-1]["artifact"].items() if k != "screenshot_png_base64"} != second_result["artifact"] or
                sha256(json.dumps(after[-1]["artifact"], separators=(",", ":")).encode()) != second_result["artifact_sha256"] or
                after[-1]["requestSha256"] != saved["requestSha256"] or
                second_result["payment_intent"] == reference):
            raise RuntimeError("identical-input second purchase was not distinct")
        second_reference = second_result["payment_intent"]
        second_response = stripe.http.get(
            f"/v1/payment_intents/{second_reference}", headers={"Stripe-Version": STRIPE_VERSION},
        )
        if second_response.status_code != 200:
            raise RuntimeError(f"second PaymentIntent lookup returned {second_response.status_code}")
        second_payment = second_response.json()
        if (second_payment.get("status"), second_payment.get("amount"), second_payment.get("currency"),
                second_payment.get("livemode")) != ("succeeded", 50, "usd", False):
            raise RuntimeError("second purchase did not produce a successful test payment")
        second_list_response = stripe.http.get(
            "/v1/payment_intents", params={"created[gte]": started, "limit": 100},
            headers={"Stripe-Version": STRIPE_VERSION},
        )
        if second_list_response.status_code != 200:
            raise RuntimeError(f"second PaymentIntent list returned {second_list_response.status_code}")
        second_list = second_list_response.json()
        if second_list.get("has_more"):
            raise RuntimeError("second PaymentIntent list is incomplete")
        observed_after = [pi for pi in second_list.get("data", []) if
                          pi.get("metadata", {}).get("machine_payment") == "true" and
                          pi.get("amount") == 50 and pi.get("currency") == "usd"]
        if len(observed_after) != 2 or {pi.get("id") for pi in observed_after} != {reference, second_reference}:
            raise RuntimeError("run window did not contain exactly the two intentional MPP payments")

        manifest = {
            "timestamp_utc": now.isoformat(),
            "stripe_sample_sha": SAMPLE_SHA,
            "lockfile_sha256": sha256((MPP / "pnpm-lock.yaml").read_bytes()),
            "server_sha256": sha256((MPP / "recovery_server.ts").read_bytes()),
            "buyer_sha256": sha256((MPP / "recovery_buyer.ts").read_bytes()),
            "proxy_sha256": sha256((MPP / "response_loss_proxy.ts").read_bytes()),
            "screenshot_module_sha256": sha256((MPP / "screenshot.ts").read_bytes()),
            "fixture_sha256": sha256((MPP / "fixtures/alpha.html").read_bytes()),
            "chrome_version": subprocess.check_output([chrome, "--version"], text=True).strip(),
            "mppx_version": "0.9.2",
            "stripe_sdk_version": "22.6.2",
            "stripe_api_version": STRIPE_VERSION,
            "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
            "pnpm_version": subprocess.check_output(["pnpm", "--version"], text=True).strip(),
            "stripe_mode": "test",
            "transport": "localhost HTTP proxy with downstream TCP reset before paid response headers",
            "restart": "buyer and merchant restarted before authenticated result lookup",
        }
        trace = {
            "operation_id_sha256": sha256(operation_id.encode()),
            "request_sha256": saved["requestSha256"],
            "agreed_price_cents": saved["priceCents"],
            "agreed_currency": saved["currency"],
            "transition_statuses": [r["status"] for r in records],
            "client_error": start_result["client_error"],
            "challenge_count": start_result["challenges"],
            "token_factory_calls": start_result["token_uses"],
            "same_operation_replay_status": start_result["replay_status"],
            "same_operation_replay_received_artifact": start_result["replay_received_artifact"],
            "fault": faults[0],
            "wrong_buyer_status": intruder["wrong_buyer_status"],
            "wrong_buyer_received_artifact": intruder["wrong_buyer_received_artifact"],
            "wrong_buyer_received_receipt": intruder["wrong_buyer_received_receipt"],
            "changed_body_status": recovered["changed_body_status"],
            "wrong_route_status": recovered["wrong_route_status"],
            "recovery_status": recovered["recovery_status"],
            "repeated_recovery_status": recovered["repeated_recovery_status"],
            "repeated_same_artifact": recovered["repeated_same_artifact"],
            "artifact": recovered["artifact"],
            "artifact_sha256": recovered["artifact_sha256"],
            "screenshot_png_sha256": saved_artifact["screenshot_sha256"],
            "screenshot_bytes": len(png),
            "saved_payment_intent_sha256": sha256(reference.encode()),
            "recovered_receipt_payment_intent_sha256": sha256(recovered["receipt_reference"].encode()),
            "stripe_payment_intent_sha256": sha256(str(payment["id"]).encode()),
            "stripe_payment_status": payment["status"],
            "stripe_amount_cents": payment["amount"],
            "stripe_currency": payment["currency"],
            "stripe_livemode": payment["livemode"],
            "run_window_matching_payment_intents_before_second_purchase": len(observed),
            "second_operation_id_sha256": sha256(second_result["operation_id"].encode()),
            "second_saved_payment_intent_sha256": sha256(second_reference.encode()),
            "second_stripe_payment_intent_sha256": sha256(str(second_payment["id"]).encode()),
            "second_payment_status": second_payment["status"],
            "identical_input_distinct_operation": second_result["operation_id"] != operation_id,
            "identical_input_distinct_payment": second_reference != reference,
            "run_window_matching_payment_intents_after_second_purchase": len(observed_after),
            "recovery_process_received_spt": False,
            "buyer_received_merchant_key": False,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (out / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        return out
    finally:
        stop(proxy)
        stop(server)
        stripe.http.close()


if __name__ == "__main__":
    print(run())
