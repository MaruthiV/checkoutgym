import json
import socket
import threading
import time
from typing import Annotated

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .agents.base import IDEM_DESC, TOOLS, StepLimit, ToolRunner, merchant_result_text

DESC = {t["name"]: t["description"] for t in TOOLS}
CURRENT: ToolRunner | None = None
_state = {"port": None, "thread": None}
mcp = MCPServer("checkoutgym", instructions="Agentic Commerce Protocol checkout tools for the current purchase.")


class LineItem(BaseModel):
    id: str
    quantity: int


class Selection(BaseModel):
    type: str = "shipping"
    option_id: str
    item_ids: list[str] | None = None


class Discounts(BaseModel):
    codes: list[str]


IdemKey = Annotated[str | None, Field(description=IDEM_DESC)]


def _dispatch(name: str, args: dict) -> str:
    if CURRENT is None:
        return json.dumps({"error": "no active trial"})
    try:
        res = CURRENT.call(name, args)
    except StepLimit as e:
        return json.dumps({"error": str(e), "instruction": "Tool budget exhausted. Stop now."})
    if name == "finish":
        return "Recorded. The task is complete. Do not call any more tools."
    return merchant_result_text(res)


async def _call(name: str, args: dict) -> str:
    return await anyio.to_thread.run_sync(_dispatch, name, {k: v for k, v in args.items() if v is not None})


@mcp.tool(name="create_checkout_session", description=DESC["create_checkout_session"])
async def create_checkout_session(line_items: list[LineItem], fulfillment_details: dict | None = None, idempotency_key: IdemKey = None) -> str:
    return await _call("create_checkout_session", {"line_items": [li.model_dump() for li in line_items], "fulfillment_details": fulfillment_details, "idempotency_key": idempotency_key})


@mcp.tool(name="update_checkout_session", description=DESC["update_checkout_session"])
async def update_checkout_session(id: str, line_items: list[LineItem] | None = None, selected_fulfillment_options: list[Selection] | None = None,
                                  discounts: Discounts | None = None, idempotency_key: IdemKey = None) -> str:
    return await _call("update_checkout_session", {
        "id": id, "line_items": [li.model_dump() for li in line_items] if line_items is not None else None,
        "selected_fulfillment_options": [s.model_dump(exclude_none=True) for s in selected_fulfillment_options] if selected_fulfillment_options is not None else None,
        "discounts": discounts.model_dump() if discounts else None, "idempotency_key": idempotency_key})


@mcp.tool(name="get_checkout_session", description=DESC["get_checkout_session"])
async def get_checkout_session(id: str) -> str:
    return await _call("get_checkout_session", {"id": id})


@mcp.tool(name="complete_checkout", description=DESC["complete_checkout"])
async def complete_checkout(id: str, payment_token: Annotated[str, Field(description="The shared payment token (spt_...) you were given.")],
                            buyer: dict | None = None, idempotency_key: IdemKey = None) -> str:
    return await _call("complete_checkout", {"id": id, "payment_token": payment_token, "buyer": buyer, "idempotency_key": idempotency_key})


@mcp.tool(name="cancel_checkout", description=DESC["cancel_checkout"])
async def cancel_checkout(id: str, idempotency_key: IdemKey = None) -> str:
    return await _call("cancel_checkout", {"id": id, "idempotency_key": idempotency_key})


@mcp.tool(name="ask_user", description=DESC["ask_user"])
async def ask_user(question: str) -> str:
    return await _call("ask_user", {"question": question})


@mcp.tool(name="finish", description=DESC["finish"])
async def finish(summary: str, order_id: str | None) -> str:
    return await _call("finish", {"summary": summary, "order_id": order_id})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_running() -> int:
    if _state["port"]:
        return _state["port"]
    port = _free_port()
    app = mcp.streamable_http_app(json_response=True, stateless_http=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    _state.update(port=port, thread=t)
    return port
