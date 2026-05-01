"""Sandboxed subprocess execution for HungerLoop v0.4.1.

:class:`SandboxRunner` executes shell commands (acceptance checks) in isolated
subprocesses with timeout enforcement, output truncation, and evidence capture.
Part of invariant I-7 (sandbox isolation).

The runner delegates evidence persistence to the repository protocol (Task 14).
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SandboxRunResult(BaseModel):
    """Result of a sandboxed subprocess execution."""

    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    evidence_id: str | None = None


class SandboxRunner:
    """Execute shell commands with timeout and output limits."""

    def __init__(self, repo: Any, max_output_chars: int = 5000) -> None:
        # TODO(Task 14): tighten ``repo`` to the Repository protocol once it lands.
        """Initialize the runner.

        Args:
            repo: Repository protocol instance (provides ``save_shell_output_as_evidence``).
            max_output_chars: Maximum characters to capture from stdout/stderr.
        """
        self.repo = repo
        self.max_output_chars = max_output_chars

    async def run_argv(
        self,
        task_id: str,
        loop_id: int,
        argv: list[str],
        cwd: Path,
        timeout: int,
        evidence_label: str,
    ) -> SandboxRunResult:
        """Execute a command with timeout and capture output.

        Args:
            task_id: Task identifier for evidence tagging.
            loop_id: Loop iteration for evidence tagging.
            argv: Command and arguments (e.g., ``["pytest", "tests/"]``).
            cwd: Working directory for the subprocess.
            timeout: Maximum execution time in seconds.
            evidence_label: Human-readable label for the evidence record.

        Returns:
            Execution result with exit code, output, and evidence ID.

        Raises:
            ValueError: If ``argv`` is empty, ``timeout`` is non-positive, or
                ``cwd`` does not exist.
            FileNotFoundError: If the executable named by ``argv[0]`` is not found.
            PermissionError: If ``argv[0]`` is not executable.
            Exception: Whatever ``repo.save_shell_output_as_evidence`` raises;
                evidence-persistence failures propagate to the caller.
        """
        if not argv:
            raise ValueError("argv cannot be empty")

        if timeout <= 0:
            raise ValueError("timeout must be positive")

        if not cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {cwd}")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            timed_out = False
        except asyncio.TimeoutError:
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            else:
                proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            timed_out = True

        # NB: decode FIRST, then slice. Slicing bytes before decode could split a
        # multi-byte UTF-8 sequence and produce spurious replacement characters.
        stdout = stdout_b.decode(errors="replace")[: self.max_output_chars]
        stderr = stderr_b.decode(errors="replace")[: self.max_output_chars]
        # proc.returncode is guaranteed non-None after communicate() per asyncio docs;
        # the fallback exists only as a defensive belt-and-braces.
        exit_code = proc.returncode if proc.returncode is not None else -1

        # Evidence persistence failures propagate to the caller. The orchestrator
        # treats them as I/O errors that abort the loop iteration; partial results
        # without evidence are not recorded as a successful sandbox run.
        evidence_id: str = self.repo.save_shell_output_as_evidence(
            task_id=task_id,
            loop_id=loop_id,
            label=evidence_label,
            argv=argv,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

        return SandboxRunResult(
            argv=argv,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            evidence_id=evidence_id,
        )
