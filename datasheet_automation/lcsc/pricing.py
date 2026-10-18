"""PriceSigner: fetch a product's full RMB price ladder from LCSC's signed API.

The ladder (all quantity tiers, in-stock or not) is not in the page's SSR JSON --
the frontend fetches it via ``/cid/verify`` -> cvt -> ``/product/stock/price/sign``,
which is signed by LCSC's own Rust->wasm module and answered only over HTTP/2.

That whole chain lives in :mod:`price_chain.js` (Node: http2 + the platform's
challenge.js acw solver + runtime-fetched signing wasm). This module is the thin
Python seam over it, mirroring :class:`~.challenge.ChallengeSolver`: Python owns
orchestration, Node owns the pure signing/transport. It returns the decrypted
payload and never raises -- a WAF block or missing Node degrades to ``None`` so
the card still ships stock/price/weight/coupons without the ladder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional, Protocol

from datasheet_automation.core.app_logging import get_logger
from datasheet_automation.core.io import runtime_output_path


log = get_logger()

_CHAIN_JS = Path(__file__).with_name("price_chain.js")
_DEFAULT_CVT_CACHE = (
    Path(__file__).resolve().parents[3]
    / "07_TEMP"
    / "datasheet_automation"
    / "cache"
    / "lcsc-cvt.json"
)


class PriceSigner(Protocol):
    def fetch_ladder(self, product_id: str) -> Optional[Any]:
        """Return the decrypted price payload, or None when unavailable."""
        ...


class NodePriceSigner:
    """Run ``price_chain.js`` in Node and return its decrypted price payload."""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    @staticmethod
    def _node_command() -> Optional[str]:
        command = os.getenv("PCBAGENT_NODE_COMMAND") or "node"
        return shutil.which(command)

    def fetch_ladder(self, product_id: str) -> Optional[Any]:
        product_id = str(product_id or "").strip()
        if not product_id:
            return None
        node = self._node_command()
        if node is None:
            log.warning("price ladder: node runtime not found; skipping signed price API")
            return None
        try:
            environment = dict(os.environ)
            environment["PCBAGENT_CVT_CACHE"] = str(
                runtime_output_path(
                    environment.get("PCBAGENT_CVT_CACHE", _DEFAULT_CVT_CACHE)
                )
            )
            proc = subprocess.run(
                [node, str(_CHAIN_JS), product_id],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            log.warning("price ladder: price_chain.js timed out for %s", product_id)
            return None
        out = proc.stdout.strip()
        if not out:
            log.warning("price ladder: empty output for %s (%s)", product_id, proc.stderr[-200:])
            return None
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            log.warning("price ladder: non-JSON output for %s", product_id)
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason") if isinstance(result, dict) else "unknown"
            blocked = isinstance(result, dict) and result.get("blocked")
            log.warning("price ladder: unavailable for %s (blocked=%s, %s)", product_id, blocked, reason)
            return None
        return result.get("payload")
