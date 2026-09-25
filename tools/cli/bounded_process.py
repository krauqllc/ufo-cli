#!/usr/bin/env python3
"""Run CLI gates with bounded UTF-8 capture and whole-process-group cleanup."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
from typing import Any, Mapping


DEFAULT_MAX_STDOUT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 1024 * 1024


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def run_bounded_command(
    command: list[str],
    *,
    cwd: Path,
    input_bytes: bytes = b"",
    timeout_seconds: float,
    label: str,
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Return strict UTF-8 output or fail after bounded capture and cleanup."""
    if not command:
        raise ValueError(f"{label} command is empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(f"{label} timeout must be positive and finite")
    if max_stdout_bytes < 0 or max_stderr_bytes < 0:
        raise ValueError(f"{label} capture limits must be nonnegative")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    captured: dict[str, bytes] = {}
    over_limit: list[str] = []
    thread_errors: list[BaseException] = []
    state_lock = threading.Lock()

    def collect(name: str, stream: Any, maximum: int) -> None:
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    with state_lock:
                        if name not in over_limit:
                            over_limit.append(name)
                    terminate_process_tree(process)
                    continue
                chunks.append(chunk)
            captured[name] = b"".join(chunks)
        except BaseException as error:
            with state_lock:
                thread_errors.append(error)
            terminate_process_tree(process)
        finally:
            stream.close()

    def send_input() -> None:
        try:
            process.stdin.write(input_bytes)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            # Early refusal can close stdin before the full request arrives.
            pass
        finally:
            process.stdin.close()

    threads = [
        threading.Thread(
            target=collect,
            args=("stdout", process.stdout, max_stdout_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=collect,
            args=("stderr", process.stderr, max_stderr_bytes),
            daemon=True,
        ),
        threading.Thread(target=send_input, daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        process.wait()
    for thread in threads:
        thread.join()
    if timed_out:
        raise ValueError(f"{label} exceeded the {timeout_seconds:g}s limit")
    if over_limit:
        stream = sorted(over_limit)[0]
        maximum = max_stdout_bytes if stream == "stdout" else max_stderr_bytes
        raise ValueError(f"{label} {stream} exceeded the {maximum}-byte limit")
    if thread_errors:
        raise ValueError(f"could not capture {label} output") from thread_errors[0]
    try:
        stdout = captured.get("stdout", b"").decode("utf-8")
        stderr = captured.get("stderr", b"").decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} output was not valid UTF-8") from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def docker_image_id(image: str, *, cwd: Path, timeout_seconds: float = 30) -> str:
    """Resolve one local Docker reference to the exact immutable image ID."""
    inspected = run_bounded_command(
        ["docker", "image", "inspect", "--format={{.Id}}", image],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        label="docker image inspection",
        max_stdout_bytes=4096,
    )
    image_id = inspected.stdout.strip()
    if (
        inspected.returncode != 0
        or inspected.stderr
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    ):
        raise ValueError(f"local container image was not found or was invalid: {image}")
    return image_id
