"""Tests for the write gate: permission, confirm tokens, and `layer set`.

Every test points `AUTO_CAD_CLI_STATE_DIR` at a scratch directory. Exercising
the gate against the operator's real state would need their permission setting
to be enabled and would leave spent tokens in their ledger.
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

from auto_cad_cli import autocad, confirm, drawing  # noqa: E402
from tests.test_drawing import SAMPLE, needs_autocad  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """An isolated state directory, writes disabled as they are by default."""
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv(confirm.ENV_STATE_DIR, str(directory))
    return directory


@pytest.fixture
def writable(state):
    """The same, with the permission a human would have had to set by hand."""
    (state / "config.json").write_text('{"permission": "write"}', encoding="utf-8")
    return state


def a_scope(target: Path, **overrides):
    base = {
        "command": "layer set",
        "target": target,
        "arguments": {"name": "0", "color": 3},
        "observed": {"color": 7, "on": True, "frozen": False, "locked": False},
    }
    base.update(overrides)
    return confirm.scope(**base)


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "drawing.dwg"
    path.write_bytes(b"pretend this is a drawing")
    return path


# --- permission ---------------------------------------------------------------


def test_writes_are_disabled_unless_a_human_enabled_them(state):
    assert confirm.permission() == "read"
    with pytest.raises(autocad.EngineError) as caught:
        confirm.require_write_permission()
    assert caught.value.code == "E_FORBIDDEN"


def test_permission_reads_write_only_from_an_exact_value(state):
    (state / "config.json").write_text('{"permission": "yes please"}', encoding="utf-8")
    assert confirm.permission() == "read"
    (state / "config.json").write_text("not json at all", encoding="utf-8")
    assert confirm.permission() == "read"


def test_enabled_permission_is_honoured(writable):
    assert confirm.permission() == "write"
    confirm.require_write_permission()


# --- confirm tokens -----------------------------------------------------------


def test_a_freshly_minted_token_verifies_once(writable, target):
    scope = a_scope(target)
    token = confirm.mint(scope)
    assert token.value.startswith(confirm.TOKEN_PREFIX)
    confirm.verify_and_consume(token.value, scope)


def test_a_token_cannot_be_spent_twice(writable, target):
    """The ledger is what makes a post-commit timeout safe to reason about."""
    scope = a_scope(target)
    token = confirm.mint(scope)
    confirm.verify_and_consume(token.value, scope)
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume(token.value, scope)
    assert caught.value.code == "E_CONFLICT"
    assert "already been used" in caught.value.message


def test_a_token_is_void_when_the_arguments_change(writable, target):
    token = confirm.mint(a_scope(target))
    other = a_scope(target, arguments={"name": "0", "color": 4})
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume(token.value, other)
    assert caught.value.code == "E_CONFLICT"


def test_a_token_is_void_when_the_drawing_changed(writable, target):
    """The file digest is in the scope, so an edit in between voids the preview."""
    token = confirm.mint(a_scope(target))
    target.write_bytes(b"somebody else edited this drawing")
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume(token.value, a_scope(target))
    assert caught.value.code == "E_CONFLICT"


def test_a_token_is_void_when_the_observed_state_moved(writable, target):
    token = confirm.mint(a_scope(target))
    moved = a_scope(target, observed={"color": 2, "on": True, "frozen": False, "locked": False})
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume(token.value, moved)
    assert caught.value.code == "E_CONFLICT"


def test_an_expired_token_is_refused(writable, target):
    scope = a_scope(target)
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume("ct_" + "0" * 32 + ".2000-01-01T00:00:00Z", scope)
    assert caught.value.code == "E_CONFLICT"
    assert "expired" in caught.value.message


@pytest.mark.parametrize("forged", ["", "nonsense", "ct_missing_expiry", "ct_" + "a" * 32])
def test_a_forged_token_is_refused(writable, target, forged):
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume(forged, a_scope(target))
    assert caught.value.code == "E_CONFLICT"


def test_a_token_cannot_be_minted_with_another_machines_key(writable, target, monkeypatch):
    """Forging needs the local secret, not just the public inputs."""
    scope = a_scope(target)
    token = confirm.mint(scope)
    monkeypatch.setattr(confirm, "_secret", lambda: b"a different machine's key")
    with pytest.raises(autocad.EngineError) as caught:
        confirm.verify_and_consume(token.value, scope)
    assert caught.value.code == "E_CONFLICT"


# --- CLI boundary -------------------------------------------------------------


def run(*args: str, env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    result = subprocess.run(
        [sys.executable, "-m", "auto_cad_cli.main", *args],
        capture_output=True,
        timeout=300,
        env=env,
        cwd=ROOT,
    )
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def test_layer_set_is_refused_without_permission(state, target):
    code, stdout = run(
        "layer",
        "set",
        "--file",
        str(target),
        "--names",
        "0",
        "--color",
        "3",
        "--dry-run",
        "--compact",
        env_extra={confirm.ENV_STATE_DIR: str(state)},
    )
    assert code == 4
    assert json.loads(stdout)["error"]["code"] == "E_FORBIDDEN"


def test_layer_set_requires_a_layer_name(state, target):
    code, stdout = run(
        "layer",
        "set",
        "--file",
        str(target),
        "--color",
        "3",
        "--compact",
        env_extra={confirm.ENV_STATE_DIR: str(state)},
    )
    assert code == 2
    assert json.loads(stdout)["error"]["details"]["flag"] == "--names"


def test_opposite_switches_are_a_usage_error(state, target):
    code, stdout = run(
        "layer",
        "set",
        "--file",
        str(target),
        "--names",
        "0",
        "--on",
        "--off",
        "--compact",
        env_extra={confirm.ENV_STATE_DIR: str(state)},
    )
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "E_USAGE"


def test_reference_marks_layer_set_as_a_write():
    code, stdout = run("reference", "--command", "layer set", "--compact")
    assert code == 0
    command = json.loads(stdout)["data"]["commands"][0]
    assert command["type"] == "write"
    # An agent must be able to see the two-step recipe without guessing.
    assert any("--dry-run" in example for example in command["examples"])
    assert any("--confirm" in example for example in command["examples"])


def test_doctor_reports_the_write_permission(state):
    code, stdout = run("doctor", "--compact", env_extra={confirm.ENV_STATE_DIR: str(state)})
    assert code == 0
    checks = {c["check"]: c for c in json.loads(stdout)["data"]["checks"]}
    assert checks["write_permission"]["status"] == "warn"
    assert checks["write_permission"]["message"] == "read"


# --- the real thing -----------------------------------------------------------


@needs_autocad
def test_layer_set_applies_and_verifies_against_a_real_drawing(writable, tmp_path):
    """The whole gate, end to end, on a copy nobody else is using."""
    work = tmp_path / "work.dwg"
    work.write_bytes(SAMPLE.read_bytes())

    before = {layer["name"]: layer for layer in drawing.layers(work)["layers"]}
    name = "0"
    wanted = 5 if before[name]["color"] != 5 else 6

    preview = drawing.set_layers(work, [name], color=wanted, dry_run=True)
    assert preview["preview"]["changes"], preview
    assert preview["preview"]["total"] == 1
    assert preview["preview"]["unresolved"] == []

    applied = drawing.set_layers(work, [name], color=wanted, confirm_token=preview["confirm_token"])
    assert applied["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
    assert applied["items"][0]["ok"] is True
    assert applied["items"][0]["after"]["color"] == wanted
    # Verification re-opened the file rather than trusting the write script,
    # which is what catches a save that silently did nothing.
    assert applied["verification"]["level"] == "reopened-and-compared"
    assert applied["verification"]["matches"] is True
    assert Path(applied["backup"]).is_file()

    # And the change is genuinely on disk, not just in the returned payload.
    assert drawing.layers(work)["layers"][0]["color"] == wanted


@needs_autocad
def test_a_batch_changes_every_named_layer_in_one_pass(writable, tmp_path):
    """Several layers, one token, one aggregated result (CLI-SPEC section 15)."""
    work = tmp_path / "work.dwg"
    work.write_bytes(SAMPLE.read_bytes())
    names = [layer["name"] for layer in drawing.layers(work)["layers"]][:3]
    assert len(names) >= 2, "sample drawing is too small for a batch test"

    preview = drawing.set_layers(work, names, locked=True, dry_run=True)
    assert preview["preview"]["targets"] == names
    assert preview["preview"]["total"] == len(names)

    applied = drawing.set_layers(work, names, locked=True, confirm_token=preview["confirm_token"])
    assert applied["summary"]["total"] == len(names)
    assert applied["summary"]["succeeded"] == len(names)
    assert [item["target"] for item in applied["items"]] == names

    after = {layer["name"]: layer for layer in drawing.layers(work)["layers"]}
    assert all(after[name]["locked"] for name in names)


@needs_autocad
def test_an_unknown_target_fails_only_itself(writable, tmp_path):
    """One typo must not hide the result of every other target."""
    work = tmp_path / "work.dwg"
    work.write_bytes(SAMPLE.read_bytes())
    real = drawing.layers(work)["layers"][0]["name"]
    names = [real, "no-such-layer"]

    preview = drawing.set_layers(work, names, locked=True, dry_run=True)
    assert preview["preview"]["unresolved"] == ["no-such-layer"]

    applied = drawing.set_layers(work, names, locked=True, confirm_token=preview["confirm_token"])
    assert applied["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    by_target = {item["target"]: item for item in applied["items"]}
    assert by_target[real]["ok"] is True
    assert by_target["no-such-layer"]["error"]["code"] == "E_NOT_FOUND"
    # Top-level success: the batch ran; per-item status lives in items[].
    assert applied["verification"]["matches"] is False


@needs_autocad
def test_layer_set_without_a_token_asks_for_a_dry_run(writable, tmp_path):
    work = tmp_path / "work.dwg"
    work.write_bytes(SAMPLE.read_bytes())
    with pytest.raises(autocad.EngineError) as caught:
        drawing.set_layers(work, ["0"], color=3)
    assert caught.value.code == "E_CONFIRMATION_REQUIRED"


@needs_autocad
def test_changing_the_target_set_voids_the_token(writable, tmp_path):
    """The token binds the whole resolved batch, not just the arguments."""
    work = tmp_path / "work.dwg"
    work.write_bytes(SAMPLE.read_bytes())
    names = [layer["name"] for layer in drawing.layers(work)["layers"]][:2]
    preview = drawing.set_layers(work, names, locked=True, dry_run=True)
    with pytest.raises(autocad.EngineError) as caught:
        drawing.set_layers(work, names[:1], locked=True, confirm_token=preview["confirm_token"])
    assert caught.value.code == "E_CONFLICT"


def test_an_empty_target_list_is_a_validation_error(writable, target):
    with pytest.raises(autocad.EngineError) as caught:
        drawing.set_layers(target, [], color=3, dry_run=True)
    assert caught.value.code == "E_VALIDATION"


def test_nothing_to_change_is_a_validation_error(writable, target):
    with pytest.raises(autocad.EngineError) as caught:
        drawing.set_layers(target, ["0"], dry_run=True)
    assert caught.value.code == "E_VALIDATION"


def test_colour_outside_the_index_is_rejected(writable, target):
    with pytest.raises(autocad.EngineError) as caught:
        drawing.set_layers(target, ["0"], color=999, dry_run=True)
    assert caught.value.code == "E_VALIDATION"


def test_duplicate_targets_collapse_but_keep_order(writable, target, monkeypatch):
    """Order is what an agent maps results back onto."""
    assert drawing._resolve_targets(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
