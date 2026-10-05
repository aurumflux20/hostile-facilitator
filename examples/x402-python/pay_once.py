"""One purchase through the official x402 Python SDK, for `hostile-facilitator test`.

Seller: FastAPI + x402 payment middleware, facilitator = $FACILITATOR_URL.
Buyer:  x402HttpxClient with a throwaway key (no funds; nothing touches a chain).

The seller's facilitator timeout is set from $HOSTILE_CLIENT_TIMEOUT, so the
timeout modes really time out. Everything else is the SDK's default behaviour.

    pip install "x402[fastapi,evm,clients]" uvicorn
    hostile-facilitator test -- python examples/x402-python/pay_once.py
"""
import asyncio
import os
import socket
import sys
import threading
import time

import uvicorn
from eth_account import Account
from fastapi import FastAPI

from x402 import x402Client, x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.clients import x402HttpxClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import register_exact_evm_client, register_exact_evm_server
from x402.mechanisms.evm.signers import EthAccountSigner

NETWORK = "eip155:84532"
PAY_TO = "0x000000000000000000000000000000000000dEaD"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seller(port: int) -> uvicorn.Server:
    facilitator = HTTPFacilitatorClient(FacilitatorConfig(
        url=os.environ["FACILITATOR_URL"],
        timeout=float(os.environ.get("HOSTILE_CLIENT_TIMEOUT", "5")),
    ))
    server = x402ResourceServer(facilitator)
    register_exact_evm_server(server, NETWORK)
    routes = {"GET /paid": {"accepts": {"scheme": "exact", "payTo": PAY_TO,
                                        "price": "$0.01", "network": NETWORK}}}
    app = FastAPI()
    mw = payment_middleware(routes, server)

    @app.middleware("http")
    async def x402_mw(request, call_next):
        return await mw(request, call_next)

    @app.get("/paid")
    async def paid():
        return {"ok": True}

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            return srv
        time.sleep(0.05)
    raise SystemExit("seller did not start")


async def _buy(port: int) -> int:
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(Account.create()))
    async with x402HttpxClient(client, timeout=60) as http:
        r = await http.get(f"http://127.0.0.1:{port}/paid")
        return r.status_code


def main() -> int:
    port = _free_port()
    srv = _seller(port)
    try:
        status = asyncio.run(_buy(port))
        print(f"buyer got HTTP {status}")
        return 0 if status == 200 else 1
    finally:
        srv.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
