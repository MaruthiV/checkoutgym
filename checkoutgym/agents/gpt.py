import json
import os

from openai import OpenAI

from .base import TOOLS, ToolRunner, merchant_result_text
from .claude import SYSTEM, task_prompt

NAME = "gpt"
MODEL = os.environ.get("CHECKOUTGYM_OPENAI_MODEL", "gpt-5")
MAX_TURNS = 40


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in TOOLS]


def run(tools: ToolRunner, task: dict) -> None:
    client = OpenAI()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task_prompt(task)}]
    for _ in range(MAX_TURNS):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=openai_tools(), tool_choice="auto")
        usage = (resp.usage.prompt_tokens, resp.usage.completion_tokens) if resp.usage else (0, 0)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        calls = msg.tool_calls or []
        if not calls:
            tools.note("model_end", stop_reason=resp.choices[0].finish_reason, text=(msg.content or "")[:500], usage=usage)
            return
        for i, c in enumerate(calls):
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {"_raw": c.function.arguments}
            res = tools.call(c.function.name, args, usage=usage if i == 0 else None)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": merchant_result_text(res)})
        if tools.done:
            return
    tools.note("turn_cap", turns=MAX_TURNS)
