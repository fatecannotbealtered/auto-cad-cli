"""Tests for the drawing commands and the accoreconsole channel.

Split deliberately in two:

- Parsing and CLI-boundary behaviour run everywhere, including CI runners that
  have never heard of AutoCAD.
- Anything that actually starts the core engine is skipped unless a real
  install and a sample drawing are present, and is never faked. A green run on
  Linux therefore proves the contract, not the CAD integration - which is why
  `release_readiness` does not claim live evidence.
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

from auto_cad_cli import autocad, drawing  # noqa: E402
from auto_cad_cli.envelope import _project  # noqa: E402


def run(*args: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "auto_cad_cli.main", *args],
        capture_output=True,
        timeout=300,
        env=env,
        cwd=ROOT,
    )
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _sample_drawing() -> Path | None:
    install = autocad.preferred_install()
    if install is None or install.accoreconsole is None:
        return None
    sample_root = install.location / "Sample"
    if not sample_root.is_dir():
        return None
    for candidate in sorted(sample_root.rglob("*.dwg")):
        return candidate
    return None


SAMPLE = _sample_drawing()
needs_autocad = pytest.mark.skipif(
    SAMPLE is None,
    reason="no local AutoCAD install with a sample drawing; the engine is never faked",
)


# --- record parsing (no engine) ----------------------------------------------


def test_missing_required_record_fails_loudly():
    """A record the script could not produce must not become a plausible zero.

    `layoutlist` is unavailable in the core engine and aborts its own line; an
    earlier version defaulted the gap to 0 and reported "0 layouts" for a
    drawing that had one.
    """
    with pytest.raises(autocad.EngineError) as caught:
        drawing._require([("acadver", "25.1")], ("acadver", "dwgname"))
    assert caught.value.code == "E_SERVER"
    assert caught.value.details["missing"] == ["dwgname"]


def test_point_parsing_rejects_malformed_values():
    assert drawing._as_point("1.5,2.5,0.0") == [1.5, 2.5, 0.0]
    assert drawing._as_point("1.5,2.5") is None
    assert drawing._as_point("a,b,c") is None


def test_release_sort_prefers_the_newer_autocad():
    assert autocad._release_sort_key("R25.1") > autocad._release_sort_key("R24.3")
    assert autocad._release_sort_key("R25.1") > autocad._release_sort_key("nonsense")


def test_com_progids_never_offer_the_bare_one():
    """The bare ProgID resolves through CurVer, which can point at a dead release."""
    assert "AutoCAD.Application" not in autocad.com_progids()


def test_script_wrapper_always_opens_and_closes_the_record_file(tmp_path):
    script = autocad.build_script('(write-line "x|1" f)', tmp_path / "out.txt")
    assert script.startswith("(setq f (open ")
    assert script.rstrip().endswith("(princ)")
    assert "(close f)" in script


def test_script_is_written_in_the_codepage_the_engine_reads(tmp_path):
    """UTF-8 here does not degrade, it hangs: see `write_script`.

    The wrapper always embeds the temp output path, so on a machine whose
    account name is not ASCII every command would have blocked until timeout.
    """
    encoding = "mbcs" if os.name == "nt" else "utf-8"
    target = tmp_path / "command.scr"
    autocad.write_script(target, '(write-line "k|v" f)\n')
    assert target.read_bytes().decode(encoding)


def test_unrepresentable_script_characters_fail_instead_of_hanging(tmp_path):
    if os.name != "nt":
        pytest.skip("the codepage constraint is a Windows engine behaviour")
    try:
        "Д".encode("mbcs")
    except UnicodeEncodeError:
        pass
    else:
        pytest.skip("this machine's codepage can represent the probe character")
    with pytest.raises(autocad.EngineError) as caught:
        autocad.write_script(tmp_path / "command.scr", '(write-line "Д|v" f)\n')
    assert caught.value.code == "E_CONFIG"


def test_records_decode_from_the_system_codepage():
    """AutoLISP `write-line` emits system-codepage bytes, not UTF-8."""
    if os.name != "nt":
        pytest.skip("mbcs round-trip is a Windows behaviour")
    path = Path(os.environ["TEMP"]) / "auto-cad-cli-record-probe.txt"
    body = "BEGIN_RECORDS\r\nlayer|机械-轮廓\r\nEND_RECORDS\r\n"
    path.write_bytes(body.encode("mbcs"))
    try:
        assert autocad._read_records(path) == [("layer", "机械-轮廓")]
    finally:
        path.unlink(missing_ok=True)


# --- the untrusted marker (no engine) ----------------------------------------


def test_fields_projection_keeps_the_untrusted_marker():
    """Dropping `_untrusted` would strip the "data, not instructions" signal."""
    data = {"layers": [{"name": "0"}], "count": 1, "_untrusted": ["layers"]}
    assert _project(data, ["layers"]) == {"layers": [{"name": "0"}], "_untrusted": ["layers"]}


def test_untrusted_marker_narrows_to_surviving_fields():
    data = {"name": "a.dwg", "directory": "C:/x", "_untrusted": ["name", "directory"]}
    assert _project(data, ["directory"]) == {"directory": "C:/x", "_untrusted": ["directory"]}


def test_untrusted_marker_disappears_when_nothing_untrusted_survives():
    data = {"name": "a.dwg", "count": 2, "_untrusted": ["name"]}
    assert _project(data, ["count"]) == {"count": 2}


# --- CLI boundary (no engine) ------------------------------------------------


def test_drawing_info_requires_a_file():
    code, stdout = run("drawing", "info", "--compact")
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "E_USAGE"


def test_layer_list_requires_a_file():
    code, stdout = run("layer", "list", "--compact")
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "E_USAGE"


def test_absent_drawing_is_not_found_before_the_engine_starts():
    code, stdout = run("drawing", "info", "--file", str(ROOT / "no-such-drawing.dwg"), "--compact")
    assert code == 3
    assert json.loads(stdout)["error"]["code"] == "E_NOT_FOUND"


def test_non_integer_limit_is_a_usage_error():
    code, stdout = run("layer", "list", "--file", __file__, "--limit", "abc", "--compact")
    assert code == 2
    envelope = json.loads(stdout)
    assert envelope["error"]["code"] == "E_USAGE"
    assert envelope["error"]["details"]["flag"] == "--limit"


DRAWING_COMMANDS = (
    ("drawing", "info"),
    ("layer", "list"),
    ("entity", "summary"),
    ("block", "list"),
    ("text", "extract"),
)


@pytest.mark.parametrize("words", DRAWING_COMMANDS)
def test_every_drawing_command_requires_a_file(words):
    code, stdout = run(*words, "--compact")
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "E_USAGE"


def test_reference_declares_every_drawing_command():
    code, stdout = run("reference", "--compact")
    assert code == 0
    paths = {c["path"] for c in json.loads(stdout)["data"]["commands"]}
    assert {" ".join(w) for w in DRAWING_COMMANDS} <= paths


def test_reference_marks_external_drawing_content_untrusted():
    code, stdout = run("reference", "--compact")
    schemas = json.loads(stdout)["data"]["schemas"]
    assert schemas["layer_list"]["untrusted_fields"] == ["layers"]
    assert "name" in schemas["drawing_info"]["untrusted_fields"]


def test_doctor_reports_the_autocad_channel():
    code, stdout = run("doctor", "--compact")
    assert code == 0
    names = {c["check"] for c in json.loads(stdout)["data"]["checks"]}
    assert {"autocad_installed", "accoreconsole_present", "com_progid_registered"} <= names


# --- real engine (skipped without a local install) ---------------------------


@needs_autocad
def test_drawing_info_reads_a_real_drawing():
    code, stdout = run("drawing", "info", "--file", str(SAMPLE), "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    assert data["name"].lower().endswith(".dwg")
    assert data["format_version"]
    assert data["counts"]["layers"] >= 1
    assert data["extents"]["min"] is not None
    assert "name" in data["_untrusted"]


@needs_autocad
def test_layer_list_reads_a_real_layer_table():
    code, stdout = run("layer", "list", "--file", str(SAMPLE), "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    assert data["count"] == data["total"] >= 1
    assert "truncated" not in data
    # Layer 0 exists in every well-formed DWG.
    assert any(layer["name"] == "0" for layer in data["layers"])
    for layer in data["layers"]:
        assert set(layer) == {"name", "color", "on", "frozen", "locked", "linetype"}


@needs_autocad
def test_layer_limit_marks_the_result_truncated():
    code, stdout = run("layer", "list", "--file", str(SAMPLE), "--limit", "1", "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    if data["total"] > 1:
        assert data["truncated"] is True
        assert data["count"] == 1


@needs_autocad
def test_entity_summary_counts_by_dxf_type():
    code, stdout = run("entity", "summary", "--file", str(SAMPLE), "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    assert data["total"] == sum(data["by_type"].values())
    assert data["distinct_types"] == len(data["by_type"])
    # Descending count is what makes the histogram readable at a glance.
    counts = list(data["by_type"].values())
    assert counts == sorted(counts, reverse=True)


@needs_autocad
def test_block_list_reports_insert_counts_and_xref_state():
    code, stdout = run("block", "list", "--file", str(SAMPLE), "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    assert data["xrefs"] == sum(1 for b in data["blocks"] if b["xref"])
    for block in data["blocks"]:
        assert set(block) == {
            "name",
            "inserts",
            "anonymous",
            "has_attributes",
            "xref",
            "xref_path",
        }
        assert block["inserts"] >= 0


@needs_autocad
def test_text_extract_returns_author_content_marked_untrusted():
    code, stdout = run("text", "extract", "--file", str(SAMPLE), "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    assert data["_untrusted"] == ["items"]
    for item in data["items"]:
        assert item["type"] in {"TEXT", "MTEXT", "ATTDEF"}
        assert isinstance(item["content"], str)


@needs_autocad
def test_an_empty_drawing_returns_empty_results_not_errors():
    """A template has no entities; summing an empty AutoLISP list must not abort."""
    install = autocad.preferred_install()
    templates = sorted((install.location).rglob("acadiso.dwt")) if install else []
    template = templates[0] if templates else None
    if template is None:
        pytest.skip("no acadiso.dwt under the install to use as an empty drawing")
    code, stdout = run("entity", "summary", "--file", str(template), "--compact")
    assert code == 0, stdout
    data = json.loads(stdout)["data"]
    assert data["total"] == 0
    assert data["by_type"] == {}


@needs_autocad
def test_reading_works_when_the_temp_path_is_not_ascii(tmp_path):
    """Regression: the wrapper embeds the temp path, and %TEMP% holds the account name."""
    scratch = tmp_path / "临时目录"
    scratch.mkdir()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["TMP"] = env["TEMP"] = str(scratch)
    result = subprocess.run(
        [sys.executable, "-m", "auto_cad_cli.main", "drawing", "info", "--file", str(SAMPLE)],
        capture_output=True,
        timeout=300,
        env=env,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")


@needs_autocad
def test_reading_a_drawing_leaves_no_engine_process_behind():
    """accoreconsole blocks forever if mis-invoked; a leaked one would pin a file."""
    run("drawing", "info", "--file", str(SAMPLE), "--compact")
    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq accoreconsole.exe"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert b"accoreconsole.exe" not in listing.stdout
