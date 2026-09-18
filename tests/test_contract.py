"""Command-level tests for the machine contract (CLI-SPEC section 13).

These drive the CLI boundary as a subprocess rather than calling helpers, so
they assert what an agent actually receives: one JSON document on stdout, the
declared envelope shape, and exit codes bound to the canonical error table.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auto_cad_cli.contract_gen import (  # noqa: E402
    CODES,
    ERROR_ENVELOPE_KEYS,
    ERROR_OBJECT_KEYS,
    META_REQUIRED_KEYS,
    SCHEMA_VERSION,
    SUCCESS_ENVELOPE_KEYS,
)


def run(*args: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "auto_cad_cli.main", *args],
        capture_output=True,
        timeout=30,
        env=env,
        cwd=ROOT,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def ok_envelope(*args: str) -> dict:
    code, stdout, _ = run(*args)
    assert code == 0, stdout
    envelope = json.loads(stdout)
    assert list(envelope) == SUCCESS_ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert list(envelope["meta"]) == META_REQUIRED_KEYS
    assert isinstance(envelope["meta"]["duration_ms"], int)
    return envelope["data"]


# --- self-describing commands -------------------------------------------------


def test_reference_declares_the_required_contract_keys():
    data = ok_envelope("reference", "--compact")
    for key in ("tool", "version", "risk_tier", "release_readiness", "commands", "schemas"):
        assert key in data, key
    assert data["tool"] == "auto-cad-cli"
    assert data["risk_tier"] == "T1"


def test_reference_gives_every_leaf_a_real_schema_and_example():
    """Guards against `output_schema` silently decaying back into a stub."""
    data = ok_envelope("reference", "--compact")
    assert data["commands"], "reference enumerated zero commands"
    for command in data["commands"]:
        label = command["path"]
        schema = data["schemas"].get(command["output_schema"])
        assert schema, f"{label} resolves to no schema"
        assert schema["fields"], f"{label} has an empty schema"
        assert command["examples"], f"{label} has no runnable example"


def test_reference_release_readiness_is_declared():
    readiness = ok_envelope("reference", "--compact")["release_readiness"]
    assert readiness["level"] in ("stable", "beta", "unpublishable")
    assert readiness["fcc_status"] in ("verified", "missing", "not_applicable", "unknown")


def test_reference_command_filter_selects_one():
    data = ok_envelope("reference", "--command", "doctor", "--compact")
    assert [c["path"] for c in data["commands"]] == ["doctor"]


def test_context_reports_version_and_credential_state():
    data = ok_envelope("context", "--compact")
    assert data["version"]
    assert isinstance(data["credentials"]["configured"], bool)


def test_doctor_checks_release_readiness_and_agrees_with_reference():
    checks = ok_envelope("doctor", "--compact")["checks"]
    by_name = {c["check"]: c for c in checks}
    assert "release_readiness" in by_name, "doctor must check release_readiness"
    for check in checks:
        assert set(check) >= {"check", "status", "fix"}
        assert check["status"] in ("pass", "warn", "fail")
        if check["status"] == "pass":
            assert check["fix"] is None

    level = ok_envelope("reference", "--compact")["release_readiness"]["level"]
    expected = {"stable": "pass", "beta": "warn", "unpublishable": "fail"}[level]
    assert by_name["release_readiness"]["status"] == expected


def test_changelog_derives_entries_from_the_changelog_file():
    data = ok_envelope("changelog", "--compact")
    assert data["current_version"]
    assert data["entries"], "changelog produced no entries"
    assert any(e["changes"]["added"] for e in data["entries"])


def test_changelog_since_filters_to_newer_versions_only():
    """`Unreleased` outranks every release: it is what the running build contains."""
    data = ok_envelope("changelog", "--since", "99.0.0", "--compact")
    assert data["since"] == "99.0.0"
    released = [e["version"] for e in data["entries"] if e["version"] != "Unreleased"]
    assert released == [], f"no release is newer than 99.0.0, got {released}"


def test_version_flag_reports_the_tool_version():
    data = ok_envelope("--version", "--compact")
    assert data["tool"] == "auto-cad-cli"
    assert data["version"]


# --- global flags -------------------------------------------------------------


def test_fields_projects_only_the_requested_keys():
    data = ok_envelope("context", "--fields", "version", "--compact")
    assert list(data) == ["version"]


def test_compact_and_indented_agree_on_content():
    assert ok_envelope("context", "--compact") == ok_envelope("context")


def test_json_alias_is_accepted():
    ok_envelope("context", "--json")


def test_help_is_human_text_and_exits_zero():
    code, stdout, _ = run("--help")
    assert code == 0
    assert "Usage:" in stdout


# --- error contract -----------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code", "exit_code"),
    [
        (("nope",), "E_USAGE", 2),
        ((), "E_USAGE", 2),
        (("--format", "xml"), "E_USAGE", 2),
        (("--fields",), "E_USAGE", 2),
        (("context", "--bogus"), "E_USAGE", 2),
        (("reference", "--command", "nope"), "E_NOT_FOUND", 3),
    ],
)
def test_failures_use_the_canonical_code_and_exit(args, code, exit_code):
    status, stdout, _ = run(*args)
    envelope = json.loads(stdout)
    assert list(envelope) == ERROR_ENVELOPE_KEYS
    assert envelope["ok"] is False
    assert list(envelope["error"]) == ERROR_OBJECT_KEYS
    assert envelope["error"]["code"] == code
    assert envelope["error"]["retryable"] is CODES[code]["retryable"]
    assert status == exit_code == CODES[code]["exit"]


def test_stdout_carries_the_error_and_stays_parseable():
    """An agent parses stdout and checks `ok`; stderr is never the error channel."""
    _, stdout, stderr = run("nope")
    json.loads(stdout)
    assert stderr == ""


def test_stdout_is_utf8_without_bom_and_lf_terminated():
    result = subprocess.run(
        [sys.executable, "-m", "auto_cad_cli.main", "context", "--compact"],
        capture_output=True,
        timeout=30,
        cwd=ROOT,
    )
    assert not result.stdout.startswith(b"\xef\xbb\xbf")
    assert result.stdout.endswith(b"\n")
    assert b"\r\n" not in result.stdout
