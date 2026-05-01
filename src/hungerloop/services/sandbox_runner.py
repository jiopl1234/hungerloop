"""Sandboxed subprocess execution for HungerLoop v0.4.1.

:class:`SandboxRunner` executes shell commands (acceptance checks) in isolated
subprocesses with timeout enforcement, output truncation, and evidence capture.
Part of invariant I-7 (sandbox isolation).

The runner delegates evidence persistence to the repository protocol (Task 14).
"""
from __future__ import annotations

import asyncio
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
            ValueError: If ``argv`` is empty or ``timeout`` is non-positive.
        """
        if not argv:
            raise ValueError("argv cannot be empty")

        if timeout <= 0:
            raise ValueError("timeout must be positive")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            timed_out = True

        stdout = stdout_b.decode(errors="replace")[: self.max_output_chars]
        stderr = stderr_b.decode(errors="replace")[: self.max_output_chars]
        exit_code = proc.returncode if proc.returncode is not None else -1

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
