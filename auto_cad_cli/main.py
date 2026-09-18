"""Entry point: global flag parsing, dispatch, and the self-describing commands.

AutoCAD capability is deliberately absent at this stage. The contract is the
foundation the rest is built on (AGENT.md workflow A, step 2), so it ships and
is testable before the first drawing command exists.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from . import __version__, changelog
from .contract_gen import CODES
from .envelope import Options, Timer, emit_error, emit_ok

TOOL = "auto-cad-cli"
RISK_TIER = "T1"

# Where per-user state (permission opt-in, caches) will live once writes exist.
STATE_DIRECTORY = Path(os.path.expanduser("~")) / ".auto-cad-cli"

HELP = f"""{TOOL} {__version__} - agent-native CLI for Autodesk AutoCAD

Usage: {TOOL} <command> [flags]

Commands:
  reference [--command <path>]   Declared capabilities, schemas and error codes
  context                        Runtime environment, configuration, credentials
  doctor                         Environment and release-readiness health checks
  changelog [--since <version>]  What changed between versions

Global flags:
  --format json|text|raw   Output format (default: json)
  --json                   Compatibility alias for --format json
  --compact                Compact JSON, no indentation
  --fields <a,b,c>         Keep only these top-level fields of `data`
  --quiet                  Suppress non-error stderr output
  --version                Report the tool version
  --help                   This text

Machine-readable capabilities live in `{TOOL} reference`, not in this text.
"""

RELEASE_READINESS = {
    "level": "unpublishable",
    "fcc_required": True,
    "fcc_status": "missing",
    "mock_upstream_required": True,
    "mock_upstream_status": "missing",
    "live_smoke_required_for_stable": True,
    "live_smoke_status": "missing",
    "reason": (
        "Skeleton: the machine contract and self-describing commands exist, but no "
        "AutoCAD capability is implemented and no drawing has ever been opened."
    ),
    "required_evidence": [
        "functional_contract_coverage_100",
        "mock_upstream_contract_tests",
        "recorded_live_smoke_for_stable",
    ],
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "reference": {
        "shape": "object",
        "fields": [
            "tool",
            "version",
            "risk_tier",
            "release_readiness",
            "commands",
            "schemas",
            "error_codes",
            "exit_codes",
            "global_flags",
        ],
        "untrusted_fields": [],
    },
    "context": {
        "shape": "object",
        "fields": ["version", "credentials", "config", "environment"],
        "untrusted_fields": [],
    },
    "doctor": {"shape": "object", "fields": ["checks"], "untrusted_fields": []},
    "changelog": {
        "shape": "object",
        "fields": ["current_version", "since", "count", "entries"],
        "untrusted_fields": [],
    },
    "version": {"shape": "object", "fields": ["tool", "version"], "untrusted_fields": []},
}

COMMANDS: list[dict[str, Any]] = [
    {
        "path": "reference",
        "type": "read",
        "description": "Declare capabilities, parameters, output schemas and error codes.",
        "params": [
            {
                "name": "command",
                "type": "string",
                "required": False,
                "multiple": False,
                "description": "Restrict the listing to one command path.",
            }
        ],
        "output_schema": "reference",
        "examples": [
            f"{TOOL} reference --compact",
            f"{TOOL} reference --command doctor --compact",
        ],
    },
    {
        "path": "context",
        "type": "read",
        "description": "Report the runtime environment, configuration and credential state.",
        "params": [],
        "output_schema": "context",
        "examples": [f"{TOOL} context --compact"],
    },
    {
        "path": "doctor",
        "type": "read",
        "description": "Check the environment and report release readiness, with fixes.",
        "params": [],
        "output_schema": "doctor",
        "examples": [f"{TOOL} doctor --compact"],
    },
    {
        "path": "changelog",
        "type": "read",
        "description": "Report what changed between versions, derived from CHANGELOG.md.",
        "params": [
            {
                "name": "since",
                "type": "string",
                "required": False,
                "multiple": False,
                "description": "Only entries strictly newer than this version.",
            }
        ],
        "output_schema": "changelog",
        "examples": [
            f"{TOOL} changelog --compact",
            # Derived so the example stays runnable across bumps.
            f"{TOOL} changelog --since {__version__} --compact",
        ],
    },
]

GLOBAL_FLAGS = [
    {"name": "format", "type": "string", "values": ["json", "text", "raw"], "default": "json"},
    {"name": "json", "type": "boolean", "description": "Alias for --format json; not recommended."},
    {"name": "compact", "type": "boolean"},
    {
        "name": "fields",
        "type": "string",
        "description": "Comma-separated top-level keys of `data`. Dotted paths are not supported.",
    },
    {"name": "quiet", "type": "boolean"},
]

EXIT_CODES = {
    "0": "success",
    "1": "generic / integrity / io / unknown",
    "2": "usage / validation",
    "3": "not found",
    "4": "auth / forbidden / config",
    "5": "confirmation required",
    "6": "conflict / invalid token",
    "7": "retryable transient",
    "8": "timeout",
    "130": "interrupted by signal",
}


class UsageError(Exception):
    """A flag or command the parser cannot honour; maps to E_USAGE, exit 2."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


def parse_globals(argv: list[str]) -> tuple[Options, list[str]]:
    """Split global flags out of argv, leaving the command words and its own flags."""
    options = Options()
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--format":
            index += 1
            if index >= len(argv):
                raise UsageError("--format needs a value", flag="--format")
            if argv[index] not in ("json", "text", "raw"):
                raise UsageError(
                    "--format must be json, text or raw", flag="--format", got=argv[index]
                )
            options.format = argv[index]
        elif token == "--json":
            options.format = "json"
        elif token == "--compact":
            options.compact = True
        elif token == "--quiet":
            options.quiet = True
        elif token == "--fields":
            index += 1
            if index >= len(argv):
                raise UsageError("--fields needs a value", flag="--fields")
            options.fields = [f.strip() for f in argv[index].split(",") if f.strip()]
        else:
            rest.append(token)
        index += 1
    return options, rest


def take_value(rest: list[str], flag: str) -> str | None:
    """Pull `--flag value` out of the remaining tokens."""
    if flag not in rest:
        return None
    position = rest.index(flag)
    if position + 1 >= len(rest):
        raise UsageError(f"{flag} needs a value", flag=flag)
    value = rest[position + 1]
    del rest[position : position + 2]
    return value


def build_reference(command: str | None) -> dict[str, Any]:
    commands = COMMANDS
    if command is not None:
        commands = [c for c in COMMANDS if c["path"] == command]
        if not commands:
            raise LookupError(command)
    return {
        "tool": TOOL,
        "version": __version__,
        "risk_tier": RISK_TIER,
        "release_readiness": RELEASE_READINESS,
        "commands": commands,
        "schemas": SCHEMAS,
        "error_codes": {name: dict(spec) for name, spec in CODES.items()},
        "exit_codes": EXIT_CODES,
        "global_flags": GLOBAL_FLAGS,
    }


def build_context() -> dict[str, Any]:
    return {
        "version": __version__,
        "credentials": {
            "configured": False,
            # AutoCAD is driven locally through the user's own licensed install;
            # there is no remote account for this tool to hold a secret for.
            "kind": "none_required",
        },
        "config": {
            "permission": "read",
            "state_directory": str(STATE_DIRECTORY),
            "state_directory_present": STATE_DIRECTORY.is_dir(),
        },
        "environment": {
            "platform": platform.system(),
            "python": platform.python_version(),
            "autocad_integration": "not_implemented",
        },
    }


def build_doctor() -> dict[str, Any]:
    windows = platform.system() == "Windows"
    spec_pin = Path(__file__).resolve().parent.parent / ".agent" / "SPEC_VERSION"
    checks = [
        {
            "check": "platform_supported",
            "status": "pass" if windows else "warn",
            "fix": None
            if windows
            else "AutoCAD automation needs Windows; only offline commands run here",
        },
        {
            "check": "spec_pin_declared",
            "status": "pass" if spec_pin.is_file() else "fail",
            "fix": None
            if spec_pin.is_file()
            else "restore .agent/SPEC_VERSION and re-sync the spec",
        },
        {
            "check": "autocad_integration",
            "status": "warn",
            "fix": f"not implemented at {__version__}; no drawing command exists yet",
        },
        {
            "check": "release_readiness",
            "status": "fail",
            "fix": "implement AutoCAD commands with command-level tests before claiming beta",
        },
    ]
    return {"checks": checks}


def build_changelog(since: str | None) -> dict[str, Any]:
    entries = changelog.load()
    if since is not None:
        entries = changelog.newer_than(entries, since)
    data: dict[str, Any] = {"current_version": __version__, "entries": entries}
    if since is not None:
        data["since"] = since
    data["count"] = len(entries)
    return data


def dispatch(rest: list[str], options: Options, timer: Timer) -> int:
    if not rest:
        raise UsageError("no command given", hint="run reference for accepted commands")

    name = rest[0]
    flags = rest[1:]

    if name == "reference":
        wanted = take_value(flags, "--command")
        reject_unknown(flags)
        try:
            return emit_ok(build_reference(wanted), options, timer)
        except LookupError as unknown:
            return emit_error(
                "E_NOT_FOUND",
                f"no command named {unknown.args[0]!r}",
                options,
                timer,
                {"command": unknown.args[0], "hint": "run reference with no --command"},
            )
    if name == "context":
        reject_unknown(flags)
        return emit_ok(build_context(), options, timer)
    if name == "doctor":
        reject_unknown(flags)
        return emit_ok(build_doctor(), options, timer)
    if name == "changelog":
        since = take_value(flags, "--since")
        reject_unknown(flags)
        return emit_ok(build_changelog(since), options, timer)

    raise UsageError(
        f"unknown command {name!r}", command=name, hint="run reference for accepted commands"
    )


def reject_unknown(flags: list[str]) -> None:
    if flags:
        raise UsageError(
            "unrecognised flags", flags=flags, hint="run reference for accepted parameters"
        )


def main(argv: list[str] | None = None) -> int:
    timer = Timer()
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--help" in argv or "-h" in argv:
        sys.stdout.write(HELP)
        return 0

    try:
        options, rest = parse_globals(argv)
    except UsageError as error:
        return emit_error("E_USAGE", str(error), Options(), timer, error.details)

    if "--version" in rest:
        rest.remove("--version")
        return emit_ok({"tool": TOOL, "version": __version__}, options, timer)

    try:
        return dispatch(rest, options, timer)
    except UsageError as error:
        return emit_error("E_USAGE", str(error), options, timer, error.details)
    except KeyboardInterrupt:
        # Still hand the agent a parseable terminal envelope (CLI-SPEC section 14).
        return emit_error("E_INTERRUPTED", "cancelled by signal", options, timer)


if __name__ == "__main__":
    raise SystemExit(main())
