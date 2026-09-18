"""The machine contract surface: one JSON document on stdout, logs on stderr.

Everything an agent parses is built here, so the envelope shape, the
`E_* -> exit code -> retryable` alignment and the stdout/stderr split have a
single implementation instead of drifting per command (CLI-SPEC section 3, 4, 6).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .contract_gen import CODES, SCHEMA_VERSION, exit_for, retryable


class Options:
    """Global flags parsed before dispatch (CLI-SPEC section 2)."""

    def __init__(
        self,
        fmt: str = "json",
        compact: bool = False,
        fields: list[str] | None = None,
        quiet: bool = False,
    ) -> None:
        self.format = fmt
        self.compact = compact
        self.fields = fields or []
        self.quiet = quiet


class Timer:
    """Wall-clock source for `meta.duration_ms`, which is always emitted."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)


def log(message: str, options: Options) -> None:
    """Human-facing progress and warnings; never stdout, never parsed."""
    if not options.quiet:
        print(message, file=sys.stderr)


def _project(data: Any, fields: list[str]) -> Any:
    """`--fields` keeps only the requested top-level keys of an object payload.

    Dotted paths are deliberately not supported yet; `reference` declares the
    supported form so an agent never has to guess (CLI-SPEC section 8).
    """
    if not fields or not isinstance(data, dict):
        return data
    return {key: data[key] for key in fields if key in data}


def _write(document: dict[str, Any], options: Options) -> None:
    """Emit exactly one JSON document on stdout, UTF-8, LF, no BOM."""
    if options.compact:
        text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(document, ensure_ascii=False, indent=2)
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def emit_ok(data: Any, options: Options, timer: Timer) -> int:
    """Success envelope. Returns the process exit code (always 0)."""
    document = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "data": _project(data, options.fields),
        "meta": {"duration_ms": timer.elapsed_ms()},
    }
    _write(document, options)
    return 0


def emit_error(
    code: str,
    message: str,
    options: Options,
    timer: Timer,
    details: dict[str, Any] | None = None,
) -> int:
    """Failure envelope. Returns the exit code the canonical table binds to `code`.

    An unknown name would silently map to exit 1 and mislead an agent, so it is
    a programming error here rather than a runtime surprise.
    """
    if code not in CODES:
        raise KeyError(f"{code} is not in the canonical contract table")
    document = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "retryable": retryable(code),
        },
        "meta": {"duration_ms": timer.elapsed_ms()},
    }
    _write(document, options)
    return exit_for(code)
