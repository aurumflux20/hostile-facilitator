"""On-chain mode: the same battery, settling real EIP-3009 transfers on a local chain.

Why this exists
---------------
`hostile.py` models settlement in memory. That is honest and fast, but a finding
written from it carries a caveat — "not a live reproduction". This module removes
the caveat. The facilitator here signs a real `transferWithAuthorization`, sends
it to a local anvil node, and counts payments by reading `Transfer` logs off the
chain. Two transfers for one purchase is not an argument about control flow any
more; it is two entries in a ledger anyone can re-read.

The client-facing interface is unchanged: `fac.settle(nonce)`. Any client written
against the in-memory battery runs here with no edits — only the money underneath
becomes real.

Requirements: foundry (`anvil`, `cast`, `forge`) on PATH. No Python dependencies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .hostile import (
    ACCEPT_THEN_TIMEOUT,
    CLEAN,
    DECLARED_SAFE,
    DOUBLE_402,
    FIVE_XX_AFTER_SETTLE,
    RECONCILE_UNAVAILABLE,
    SLOW_ANSWER,
    ProviderError,
    Rechallenge,
    TimeoutError,  # noqa: A004 - the battery's own transport-timeout signal
)

# anvil's first two deterministic accounts.
PAYER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
PAYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
MERCHANT = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
RELAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "TestUSDC.sol"
AMOUNT = 1_000_000          # 1.000000 TUSDC per purchase
MINT = 1_000_000_000        # plenty, so a second charge always succeeds if attempted


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(
            f"on-chain mode needs foundry's `{tool}` on PATH. "
            "Install: curl -L https://foundry.paradigm.xyz | bash && foundryup"
        )
    return path


def _run(args: list[str], **kw) -> str:
    out = subprocess.run(args, capture_output=True, text=True, **kw)
    if out.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(out.stderr or out.stdout).strip()[:400]}")
    return out.stdout.strip()


@dataclass
class Chain:
    """A local EVM with an EIP-3009 token deployed and the payer funded."""

    port: int = 8545
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    token: str = ""
    chain_id: int = 31337

    @property
    def rpc(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Chain":
        _need("anvil"); _need("cast"); _need("forge")
        self._proc = subprocess.Popen(
            ["anvil", "--port", str(self.port), "--silent", "--chain-id", str(self.chain_id)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):                       # wait for RPC, ~10s worst case
            try:
                _run(["cast", "chain-id", "--rpc-url", self.rpc]); break
            except RuntimeError:
                time.sleep(0.1)
        else:
            self.__exit__(None, None, None)
            raise RuntimeError("anvil did not come up")

        env = {**os.environ, "FOUNDRY_DISABLE_NIGHTLY_WARNING": "1"}
        created = _run([
            "forge", "create", f"{CONTRACT}:TestUSDC", "--rpc-url", self.rpc,
            "--private-key", RELAYER_KEY, "--broadcast", "--json",
            "--out", "/tmp/hfout", "--cache-path", "/tmp/hfcache",
        ], env=env)
        # forge pretty-prints the JSON, so parse from the first brace, not the last line.
        self.token = json.loads(created[created.index("{"):])["deployedTo"]
        self._send("mint(address,uint256)", PAYER, str(MINT))
        return self

    def __exit__(self, *exc) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    # --- chain helpers -------------------------------------------------------

    def _send(self, sig: str, *args: str) -> str:
        return _run(["cast", "send", self.token, sig, *args, "--rpc-url", self.rpc,
                     "--private-key", RELAYER_KEY, "--json"])

    def call(self, sig: str, *args: str) -> str:
        return _run(["cast", "call", self.token, sig, *args, "--rpc-url", self.rpc])

    def balance(self, who: str) -> int:
        return int(self.call("balanceOf(address)(uint256)", who).split()[0])

    def nonce_used(self, nonce32: str) -> bool:
        return self.call("authorizationState(address,bytes32)(bool)", PAYER, nonce32) == "true"

    def transfers(self) -> int:
        """Ground truth: how many payer→merchant Transfer events the chain holds."""
        topic = _run(["cast", "keccak", "Transfer(address,address,uint256)"])
        pad = lambda a: "0x" + "0" * 24 + a[2:].lower()  # noqa: E731
        logs = _run(["cast", "logs", "--rpc-url", self.rpc, "--from-block", "0",
                     "--address", self.token, topic, pad(PAYER), pad(MERCHANT), "--json"])
        return len(json.loads(logs or "[]"))

    def sign_authorization(self, nonce32: str, valid_after: int, valid_before: int) -> tuple[str, str, str]:
        """Sign a real EIP-712 TransferWithAuthorization as the payer. Returns (v, r, s).

        The validity window is passed in, never re-derived: signing and submitting
        must cover byte-identical fields or `ecrecover` yields a different address
        and the chain reverts — which a caller could easily misread as "already
        settled". (Found by this rig failing intermittently on itself.)
        """
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {"name": "TestUSDC", "version": "2",
                       "chainId": self.chain_id, "verifyingContract": self.token},
            "message": {"from": PAYER, "to": MERCHANT, "value": str(AMOUNT),
                        "validAfter": str(valid_after), "validBefore": str(valid_before),
                        "nonce": nonce32},
        }
        sig = _run(["cast", "wallet", "sign", "--private-key", PAYER_KEY,
                    "--data", json.dumps(typed)])
        r, s, v = "0x" + sig[2:66], "0x" + sig[66:130], str(int(sig[130:132], 16))
        return v, r, s

    def submit(self, nonce32: str) -> bool:
        """Broadcast a transfer for `nonce32`. True if the money moved, False if the
        chain rejected it as already-used (the replay guard doing its job)."""
        now = int(time.time())
        valid_after, valid_before = now - 60, now + 3600
        v, r, s = self.sign_authorization(nonce32, valid_after, valid_before)
        try:
            _run(["cast", "send", self.token,
                  "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
                  PAYER, MERCHANT, str(AMOUNT), str(valid_after), str(valid_before), nonce32, v, r, s,
                  "--rpc-url", self.rpc, "--private-key", RELAYER_KEY, "--json"])
            return True
        except RuntimeError as e:
            # Only a *used authorization* means "the money already moved". Every other
            # revert (bad signature, expired window, insufficient balance) is a broken
            # rig, and must not be laundered into "already settled" — that would make
            # the instrument under-count exactly the thing it exists to count.
            if "authorization is used" in str(e):
                return False
            raise


def _nonce32(nonce: str) -> str:
    """Map a client's nonce string onto a bytes32 the token can key on."""
    return _run(["cast", "keccak", nonce])


@dataclass
class OnChainFacilitator:
    """Same misbehaviour as `hostile.Facilitator`, but the money is real.

    `settle(nonce)` signs and broadcasts an EIP-3009 authorization for that nonce,
    then injects the mode's failure. `distinct_payments` is read from chain logs,
    not from bookkeeping — the number cannot be argued with.
    """

    chain: Chain
    mode: str = CLEAN
    client_timeout_s: float = 1.0
    _settle_calls: int = 0

    @property
    def declares_safe_to_replay(self) -> bool:
        return self.mode == DECLARED_SAFE

    def reconcile(self, nonce: str) -> str:
        """The 'did it land?' read — an on-chain `authorizationState` check."""
        if self.mode == RECONCILE_UNAVAILABLE:
            raise ReconcileUnavailableOnChain("reconciliation read failed")
        return "settled" if self.chain.nonce_used(_nonce32(nonce)) else "absent"

    def settle(self, nonce: str) -> dict:
        self._settle_calls += 1
        moved = self.chain.submit(_nonce32(nonce))   # <-- real money, once per nonce

        if not moved:                                # recognised replay: answer cleanly
            return {"status": "settled", "replay": True}

        if self.mode == DECLARED_SAFE:
            return {"status": "settled", "replay": False}
        if self.mode == RECONCILE_UNAVAILABLE:
            raise ProviderError(504, "gateway timeout (settle may have landed)")
        if self.mode == ACCEPT_THEN_TIMEOUT:
            time.sleep(self.client_timeout_s + 0.2)
            raise TimeoutError("no response (but the settle already landed)")
        if self.mode == SLOW_ANSWER:
            time.sleep(max(0.0, self.client_timeout_s - 0.1))
            return {"status": "settled", "replay": False}
        if self.mode == FIVE_XX_AFTER_SETTLE:
            raise ProviderError(502, "bad gateway (but the settle already landed)")
        if self.mode == DOUBLE_402:
            raise Rechallenge()
        return {"status": "settled", "replay": False}

    @property
    def distinct_payments(self) -> int:
        return self.chain.transfers()


class ReconcileUnavailableOnChain(Exception):
    """The on-chain reconciliation read failed: 'could not determine', terminal."""
