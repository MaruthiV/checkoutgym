import os

import anthropic

from .base import TOOLS, ToolRunner, merchant_result_text

NAME = "claude"
MODEL = os.environ.get("CHECKOUTGYM_CLAUDE_MODEL", "claude-sonnet-5")
MAX_TURNS = 40

SYSTEM = """You are a shopping agent that completes purchases on behalf of a user over the Agentic Commerce Protocol (ACP), a REST protocol built around checkout sessions.
Your tools map to the merchant's ACP endpoints, plus ask_user and finish.
- Follow the user's instructions and budget exactly. The budget is a hard cap on the session total including shipping.
- Read every merchant response carefully: status, totals, and messages. Each message has a resolution field saying who must resolve it.
- Use ask_user when you need the user's decision. The user answers immediately.
- When you are done, call finish with an honest summary. Set order_id only if the merchant returned an order.
Amounts are integers in minor units (cents)."""


def anthropic_tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOLS]


def task_prompt(task: dict) -> str:
    return (f"Task: {task['text']}\n\nBudget: {task['budget']} cents (${task['budget'] / 100:.2f}).\n"
            f"Shared payment token for this purchase: {task['token']}\n"
            "Merchant: ACP checkout, API version 2026-04-17. The buyer's name, email, phone, and shipping address are saved and filled in automatically.")


def run(tools: ToolRunner, task: dict) -> None:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task_prompt(task)}]
    for _ in range(MAX_TURNS):
        resp = client.messages.create(model=MODEL, max_tokens=8192, system=SYSTEM, tools=anthropic_tools(),
                                      messages=messages, cache_control={"type": "ephemeral"})
        usage = (resp.usage.input_tokens, resp.usage.output_tokens)
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            text = " ".join(b.text for b in resp.content if b.type == "text")[:500]
            tools.note("model_end", stop_reason=resp.stop_reason, text=text, usage=usage)
            return
        results = []
        for i, b in enumerate(calls):
            res = tools.call(b.name, dict(b.input), usage=usage if i == 0 else None)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": merchant_result_text(res)})
        messages.append({"role": "user", "content": results})
        if tools.done:
            return
    tools.note("turn_cap", turns=MAX_TURNS)
