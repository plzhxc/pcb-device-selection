"""Default subprocess adapter: shell out to the portable bundle's mineru-run.

The bundle ships ``mineru-run``, which owns *all* runtime env setup -- first-run
conda-unpack, ``LD_LIBRARY_PATH`` (the new libstdc++ old glibc lacks), the in-tree
model config, and thread caps. So this backend does not replicate any of that:
for the default (bundle) path it just invokes ``bash mineru-run <pdf> <out> <flags>``.
That is the single source of truth for MinerU's environment; the engine's own job
is command assembly, subprocess lifetime, timeout, and process-tree cleanup.

Non-default paths kept minimal: an explicit ``command`` override (caller owns the
env) and a bare MinerU checkout (source on ``PYTHONPATH``). The old six-step
command waterfall (PATH mineru / conda envs / project venv) is gone with the
retired vendor checkout.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from datasheet_automation.core.app_logging import get_logger
from datasheet_automation.core.io import DATASHEET_RUNTIME_ROOT, ensure_dir
from .protocol import MinerURequest, MinerUResult
from .transaction import find_mineru_outputs


log = get_logger()


def _run_wrapper(source_root: Path) -> Path | None:
    """Return the bundle's ``mineru-run`` if the file is present.

    Deliberately does *not* require the exec bit: a lost mode (unzip on another
    OS, cross-fs copy, mode-stripping checkout) must not silently demote us to
    the checkout path, which has none of the wrapper's env setup and would crash
    on old glibc. We invoke it via ``bash`` in :meth:`build_command`, so presence
    is enough.
    """

    run = source_root / "mineru-run"
    return run if run.is_file() else None


def _looks_like_bundle(source_root: Path) -> bool:
    """A portable bundle carries its own ``env/`` (conda env); a bare checkout doesn't."""

    return (source_root / "env").is_dir()


class MinerUCliBackend:
    """Invoke MinerU through the bundle wrapper, an explicit command, or a checkout."""

    def parse(self, request: MinerURequest) -> MinerUResult:
        command = self.build_command(request)
        env = os.environ.copy()
        temp_root = DATASHEET_RUNTIME_ROOT.parent
        os_temp = ensure_dir(temp_root / "os-tmp")
        python_cache = ensure_dir(temp_root / "python-cache")
        xdg_cache = ensure_dir(DATASHEET_RUNTIME_ROOT / "cache")
        env.update(
            {
                "TMPDIR": str(os_temp),
                "TMP": str(os_temp),
                "TEMP": str(os_temp),
                "XDG_CACHE_HOME": str(xdg_cache),
                "PYTHONPYCACHEPREFIX": str(python_cache),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        # Only the bare-checkout path needs PYTHONPATH; the mineru-run path has
        # mineru in its env's site-packages and sets everything itself, and an
        # explicit command override is the caller's responsibility.
        explicit = request.command or os.getenv("PCBAGENT_MINERU_COMMAND")
        if (
            not explicit
            and _run_wrapper(request.source_root) is None
            and request.source_root.exists()
        ):
            current = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{request.source_root}:{current}" if current else str(request.source_root)
            )

        log.debug("mineru subprocess | %s", " ".join(command))
        started = time.monotonic()
        completed = self._run_process(
            command,
            env=env,
            timeout_s=request.timeout_s,
            cwd=request.output_dir,
        )
        duration_s = round(time.monotonic() - started, 3)
        log.info(
            "mineru parse done | %s | rc=%s | %.1fs",
            request.pdf_path.name,
            completed.returncode,
            duration_s,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "MinerU failed with exit code "
                f"{completed.returncode}.\nCommand: {' '.join(command)}\n"
                f"STDERR:\n{completed.stderr[-4000:]}\n"
                f"STDOUT:\n{completed.stdout[-4000:]}"
            )

        result = find_mineru_outputs(
            request.output_dir,
            request.pdf_path.stem,
            request.method,
        )
        if result is None:
            raise RuntimeError(
                "MinerU finished but no exact "
                f"{request.pdf_path.stem}_content_list.json was found under "
                f"{request.output_dir}"
            )
        result.command = command
        result.duration_s = duration_s
        result.stdout_tail = completed.stdout[-4000:]
        result.stderr_tail = completed.stderr[-4000:]
        return result

    @staticmethod
    def build_command(request: MinerURequest) -> list[str]:
        explicit = request.command or os.getenv("PCBAGENT_MINERU_COMMAND")
        run = _run_wrapper(request.source_root)
        if explicit:
            # Caller supplies the launcher (and its env); we append PDF/out + flags.
            command = shlex.split(explicit)
            command += ["-p", str(request.pdf_path), "-o", str(request.output_dir)]
        elif run is not None:
            # Default: the bundle wrapper takes <pdf> <out> positionally and owns
            # the env. Invoke via ``bash`` so a lost exec bit can't break us. Our
            # flags come after its hardcoded defaults; click resolves a duplicated
            # option to the last value, so ours win.
            command = ["bash", str(run), str(request.pdf_path), str(request.output_dir)]
        elif _looks_like_bundle(request.source_root):
            # A bundle (has env/) but no mineru-run: refuse loudly rather than
            # silently falling to the checkout path, which lacks the wrapper's
            # LD_LIBRARY_PATH / model config / conda-unpack and would crash on old
            # glibc or re-download models.
            raise RuntimeError(
                "Portable bundle at "
                f"{request.source_root} has env/ but no usable mineru-run; "
                "the bundle is incomplete. Restore mineru-run or pass an explicit "
                "command override."
            )
        else:
            # Bare MinerU checkout (source on PYTHONPATH in parse()).
            command = [sys.executable, "-m", "mineru.cli.client",
                       "-p", str(request.pdf_path), "-o", str(request.output_dir)]
        command += [
            "-b", request.backend,
            "-m", request.method,
            "-l", request.lang,
            "-f", str(bool(request.formula)),
            "-t", str(bool(request.table)),
        ]
        if request.api_url:
            command += ["--api-url", request.api_url]
        if request.server_url:
            command += ["-u", request.server_url]
        return command

    @staticmethod
    def _run_process(
        command: list[str],
        *,
        env: dict[str, str],
        timeout_s: float | None,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"MinerU command not found: {command[0]}. "
                "Pass a command override or check the portable bundle."
            ) from exc

        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            raise RuntimeError(
                f"MinerU timed out after {timeout_s:g}s. Command: {' '.join(command)}. "
                "The MinerU process group was terminated and staged output was discarded."
            ) from exc
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout or "",
            stderr or "",
        )


def _descendant_pids(root_pid: int) -> list[int]:
    """Best-effort descendant PIDs via /proc, captured before termination.

    MinerU's client starts its worker in a *separate* session, so a plain
    process-group kill misses it. We read the ppid chain while the tree is still
    intact (children re-parent to init once a parent dies). Non-Linux or a race
    just yields a shorter list.
    """

    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []
    children: dict[int, list[int]] = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        fields = data[data.rfind(b")") + 2:].split()
        if len(fields) < 2:
            continue
        try:
            children.setdefault(int(fields[1]), []).append(pid)
        except ValueError:
            continue
    out: list[int] = []
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate MinerU and every descendant, then drain without hanging.

    Guards two hazards: (1) MinerU launches its worker in a separate session, so
    killing only the client's group orphans that worker -- it keeps burning CPU
    on a shared node; (2) the orphan inherits the stdout/stderr pipe write ends,
    so an unbounded ``communicate()`` would block on an EOF that never arrives.
    So we signal the group *and* every descendant captured up front, bound every
    drain, and release the pipes if a straggler still holds them.
    """

    if process.poll() is not None:
        return
    descendants = _descendant_pids(process.pid) if os.name == "posix" else []

    def _signal_all(sig: int) -> None:
        if os.name != "posix":
            process.terminate() if sig == signal.SIGTERM else process.kill()
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in descendants:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    _signal_all(signal.SIGTERM)
    try:
        process.communicate(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_all(signal.SIGKILL)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
