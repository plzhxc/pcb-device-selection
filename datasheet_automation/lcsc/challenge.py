"""ChallengeSolver: solve an LCSC JS cookie challenge, return the cookies it sets.

Both LCSC hosts guard content with "the page computes a cookie and reloads": the
item-detail Aliyun WAF (``acw_sc__v2``) and the search host's RC4 (``tws2_<n>``).
Two adapters satisfy one interface -- ``solve(html, url) -> {cookie: value}``:

- :class:`NodeSolver` runs the page's own challenge scripts in a Node sandbox
  (robust to obfuscation changes); handles both hosts.
- :class:`Rc4Solver` reimplements the search-host RC4 in pure Python as a fallback
  when Node is unavailable.

Neither adapter touches any HTTP session -- they return cookies, and the caller
sets them. That keeps solving pure and both paths testable on fixture HTML.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from datasheet_automation.core.app_logging import get_logger


log = get_logger()


class NodeUnavailableError(RuntimeError):
    """Raised when no usable Node runtime is found for the WAF solver."""


class ChallengeSolver(Protocol):
    def solve(self, html: str, url: str, *, user_agent: str = "") -> dict[str, str]:
        ...


_SOLVER_JS = Path(__file__).with_name("challenge.js")
_RENDER_DATA_RE = re.compile(r'<textarea[^>]*id="renderData"[^>]*>(.*?)</textarea>', re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL)


class NodeSolver:
    """Run a challenge page's scripts in Node and return every cookie it sets."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @staticmethod
    def _node_command() -> str:
        command = os.getenv("PCBAGENT_NODE_COMMAND") or "node"
        resolved = shutil.which(command)
        if resolved is None:
            raise NodeUnavailableError(
                f"node runtime not found (command={command!r}); "
                "set PCBAGENT_NODE_COMMAND or install Node to solve the LCSC WAF"
            )
        return resolved

    def solve(self, html: str, url: str, *, user_agent: str = "") -> dict[str, str]:
        scripts = _SCRIPT_RE.findall(html)
        if len(scripts) < 2:
            return {}
        render = _RENDER_DATA_RE.search(html)
        payload = json.dumps({
            "scripts": scripts,
            "renderData": render.group(1) if render else "",
            "url": url,
            "userAgent": user_agent,
        })
        proc = subprocess.run(
            [self._node_command(), str(_SOLVER_JS)],
            input=payload, capture_output=True, text=True, timeout=self.timeout,
        )
        out = proc.stdout.strip()
        if not out:
            return {}
        try:
            cookies = json.loads(out)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in cookies.items()} if isinstance(cookies, dict) else {}


class Rc4Solver:
    """Pure-Python fallback for the search-host RC4 (``tws2_<n>``) challenge."""

    def solve(self, html: str, url: str, *, user_agent: str = "") -> dict[str, str]:
        try:
            xvasu = re.search(r"var\s+_xvasu\s*=\s*([0-9]+)\s*;", html)
            xvpts = re.search(r"var\s+_xvpts\s*=\s*([0-9.]+)\s*;", html)
            xvpfs = re.search(r'var\s+_xvpfs\s*=\s*"([^"]+)"\s*;', html)
            if not (xvasu and xvpts and xvpfs):
                return {}
            key = self._extract_challenge_key(html)
            payload = f"{xvpts.group(1)}:{xvasu.group(1)}"
            value = base64.b64encode(
                self._rc4_bytes(key.encode("utf-8"), payload.encode("utf-8"))
            ).decode("ascii")
            cookie_name = f"{xvpfs.group(1)}{xvasu.group(1)}"
            return {cookie_name: value}
        except Exception as exc:
            log.debug("RC4 challenge solve failed (returning no cookies): %s", exc)
            return {}

    @staticmethod
    def _extract_challenge_key(html: str) -> str:
        match = re.search(r"'([^']{6,64}\*[^']*)'", html)
        return match.group(1) if match else "tg09It3*9h"

    @staticmethod
    def _rc4_bytes(key: bytes, data: bytes) -> bytes:
        state = list(range(256))
        cursor = 0
        for idx in range(256):
            cursor = (cursor + state[idx] + key[idx % len(key)]) % 256
            state[idx], state[cursor] = state[cursor], state[idx]
        out = bytearray()
        idx = 0
        cursor = 0
        for byte in data:
            idx = (idx + 1) % 256
            cursor = (cursor + state[idx]) % 256
            state[idx], state[cursor] = state[cursor], state[idx]
            out.append(byte ^ state[(state[idx] + state[cursor]) % 256])
        return bytes(out)
