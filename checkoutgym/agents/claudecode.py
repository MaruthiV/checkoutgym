import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

from .. import mcp_server
from .base import TOOLS, ToolRunner
from .claude import SYSTEM, task_prompt

NAME = "claudecode"
MODEL = os.environ.get("CHECKOUTGYM_CLAUDECODE_MODEL", "sonnet")
SERVER = "checkoutgym"
TOOL_NAMES = [f"mcp__{SERVER}__{t['name']}" for t in TOOLS]
TRIAL_TIMEOUT_S = 900
GRACE_AFTER_FINISH_S = 20
SYSTEM_CC = SYSTEM + "\nYour tools are provided by an MCP server and are named mcp__checkoutgym__<tool>. You have no other tools."


def run(tools: ToolRunner, task: dict) -> None:
    port = mcp_server.ensure_running()
    mcp_server.CURRENT = tools
    workdir = tempfile.mkdtemp(prefix="checkoutgym-cc-")
    cfg = os.path.join(workdir, "mcp.json")
    with open(cfg, "w") as f:
        json.dump({"mcpServers": {SERVER: {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}}, f)
    # nested claude code refuses to start with the parent session's env vars present
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    cmd = [shutil.which("claude") or "claude", task_prompt(task), "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
           "--system-prompt", SYSTEM_CC, "--tools", "", "--mcp-config", cfg, "--strict-mcp-config",
           "--allowedTools", ",".join(TOOL_NAMES), "--no-session-persistence"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, cwd=workdir, env=env)
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    t0, done_at, killed = time.time(), None, None
    while proc.poll() is None:
        if tools.done and done_at is None:
            done_at = time.time()
        if done_at and time.time() - done_at > GRACE_AFTER_FINISH_S:
            proc.terminate()
            killed = "terminated after finish"
            break
        if time.time() - t0 > TRIAL_TIMEOUT_S:
            proc.kill()
            killed = "timeout"
            break
        time.sleep(0.3)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    reader.join(timeout=5)
    stderr = proc.stderr.read()[-2000:]
    mcp_server.CURRENT = None
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    usage = result.get("usage") or {}
    tokens_in = sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    tokens_out = usage.get("output_tokens", 0) or 0
    mcp_status = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    tools.billing = "subscription"
    tools.usd_api_equivalent = result.get("total_cost_usd")
    tools.model_used = init.get("model") or result.get("model")
    tools.note("claude_code", usage=(tokens_in, tokens_out), model=tools.model_used, turns=result.get("num_turns"),
               subtype=result.get("subtype"), is_error=result.get("is_error"), usd_api_equivalent=tools.usd_api_equivalent,
               mcp=mcp_status, killed=killed, exit=proc.returncode, final_text=str(result.get("result", ""))[:500])
    if mcp_status.get(SERVER) not in ("connected", None) or (not events and proc.returncode):
        raise RuntimeError(f"claude code failed: mcp={mcp_status} exit={proc.returncode} stderr={stderr[-600:]}")
    if not init:
        raise RuntimeError(f"claude code produced no init event: exit={proc.returncode} stderr={stderr[-600:]}")
