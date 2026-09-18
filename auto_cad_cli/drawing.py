"""Drawing commands, served by the headless core engine.

Reads open the drawing `/readonly` in a separate accoreconsole process, so they
can never disturb a drawing the operator has open in the GUI, and none of them
needs an AutoCAD window at all. `set_layers` is the one write, and it goes
through the gate in `confirm` before touching anything.

Content read out of a DWG - layer names, linetype names, the drawing name - is
external data authored by whoever produced the file. It is reported under
`_untrusted` so an agent treats it as data and never as instruction
(SEC-SPEC section 2).
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any

from . import autocad, confirm

# INSUNITS (DXF $INSUNITS). Only the values a mechanical or architectural
# drawing realistically carries are named; anything else is reported by number.
INSUNITS = {
    0: "unitless",
    1: "inches",
    2: "feet",
    3: "miles",
    4: "millimeters",
    5: "centimeters",
    6: "meters",
    7: "kilometers",
    8: "microinches",
    9: "mils",
    10: "yards",
    13: "microns",
    14: "decimeters",
    15: "decameters",
    16: "hectometers",
    21: "us-survey-feet",
}

# DXF group 70 on a LAYER table record.
_FROZEN_BIT = 1
_LOCKED_BIT = 4

_INFO_BODY = """
(write-line (strcat "acadver|" (getvar "ACADVER")) f)
(write-line (strcat "dwgname|" (getvar "DWGNAME")) f)
(write-line (strcat "dwgprefix|" (getvar "DWGPREFIX")) f)
(write-line (strcat "insunits|" (itoa (getvar "INSUNITS"))) f)
(write-line (strcat "measurement|" (itoa (getvar "MEASUREMENT"))) f)
(defun pt3 (key p)
  (write-line
    (strcat key "|" (rtos (car p) 2 6)
            "," (rtos (cadr p) 2 6)
            "," (rtos (caddr p) 2 6))
    f))
(pt3 "extmin" (getvar "EXTMIN"))
(pt3 "extmax" (getvar "EXTMAX"))
(setq d (dictsearch (namedobjdict) "ACAD_LAYOUT"))
(foreach pair d
  (if (= (car pair) 3) (write-line (strcat "layout|" (cdr pair)) f)))
(setq n 0 e (tblnext "LAYER" T))
(while e (setq n (1+ n) e (tblnext "LAYER")))
(write-line (strcat "layers|" (itoa n)) f)
(setq n 0 e (tblnext "BLOCK" T))
(while e (setq n (1+ n) e (tblnext "BLOCK")))
(write-line (strcat "blocks|" (itoa n)) f)
"""

# Counts are accumulated inside AutoLISP rather than emitted per entity: a real
# drawing holds tens of thousands of objects, and one record each would turn a
# summary into a transfer.
_ENTITY_BODY = """
(setq counts '())
(setq e (entnext))
(while e
  (setq ty (cdr (assoc 0 (entget e))))
  (setq hit (assoc ty counts))
  (if hit
    (setq counts (subst (cons ty (1+ (cdr hit))) hit counts))
    (setq counts (cons (cons ty 1) counts)))
  (setq e (entnext e)))
(foreach c counts
  (write-line (strcat "entity|" (car c) "\\t" (itoa (cdr c))) f))
(write-line (strcat "scanned|" (itoa (apply '+ (mapcar 'cdr counts)))) f)
"""

# Block definitions plus how many times each is actually placed. An xref is a
# block record carrying group 1 (the referenced path).
_BLOCK_BODY = """
(setq uses '())
(setq e (entnext))
(while e
  (setq d (entget e))
  (if (= (cdr (assoc 0 d)) "INSERT")
    (progn
      (setq nm (cdr (assoc 2 d)) hit (assoc nm uses))
      (if hit
        (setq uses (subst (cons nm (1+ (cdr hit))) hit uses))
        (setq uses (cons (cons nm 1) uses)))))
  (setq e (entnext e)))
(setq b (tblnext "BLOCK" T))
(while b
  (setq nm (cdr (assoc 2 b)) hit (assoc nm uses))
  (write-line
    (strcat "block|" nm
            "\\t" (itoa (cdr (assoc 70 b)))
            "\\t" (itoa (if hit (cdr hit) 0))
            "\\t" (if (assoc 1 b) (cdr (assoc 1 b)) ""))
    f)
  (setq b (tblnext "BLOCK")))
"""

# Text content is written last on the record so a tab inside it cannot shift the
# preceding fields.
_TEXT_BODY = """
(setq e (entnext))
(while e
  (setq d (entget e) ty (cdr (assoc 0 d)))
  (if (member ty '("TEXT" "MTEXT" "ATTDEF"))
    (write-line
      (strcat "text|" ty
              "\\t" (cdr (assoc 8 d))
              "\\t" (rtos (car (cdr (assoc 10 d))) 2 4)
              "," (rtos (cadr (cdr (assoc 10 d))) 2 4)
              "\\t" (if (assoc 1 d) (cdr (assoc 1 d)) ""))
      f))
  (setq e (entnext e)))
"""

# A layout object carries AcDbPlotSettings and AcDbLayout concatenated, so group
# 1 appears twice - first the (often empty) page setup name, then the layout
# name. `assoc` would return the wrong one, so the name comes from the
# dictionary key instead.
_LAYOUT_BODY = """
(defun g (code lst dflt)
  (if (assoc code lst) (cdr (assoc code lst)) dflt))
(setq d (dictsearch (namedobjdict) "ACAD_LAYOUT"))
(setq nm nil)
(foreach pair d
  (cond
    ((= (car pair) 3) (setq nm (cdr pair)))
    ((= (car pair) 350)
      (setq L (entget (cdr pair)))
      (write-line
        (strcat "layout|" nm
                "\\t" (itoa (g 71 L 0))
                "\\t" (g 2 L "")
                "\\t" (g 4 L "")
                "\\t" (rtos (g 44 L 0.0) 2 3)
                "\\t" (rtos (g 45 L 0.0) 2 3)
                "\\t" (rtos (g 142 L 0.0) 2 6)
                "\\t" (rtos (g 143 L 0.0) 2 6)
                "\\t" (itoa (g 75 L 0)))
        f))))
"""

# xref state lives in the BLOCK table record's flags; whether the referenced
# file is actually there is a filesystem question answered on the Python side.
_XREF_BODY = """
(setq b (tblnext "BLOCK" T))
(while b
  (if (assoc 1 b)
    (write-line
      (strcat "xref|" (cdr (assoc 2 b))
              "\\t" (itoa (cdr (assoc 70 b)))
              "\\t" (cdr (assoc 1 b)))
      f))
  (setq b (tblnext "BLOCK")))
"""

# Emits one record per layer; fields are tab-separated because a layer name may
# legally contain almost anything except a tab.
_LAYER_BODY = """
(setq e (tblnext "LAYER" T))
(while e
  (write-line
    (strcat "layer|"
      (cdr (assoc 2 e)) "\\t"
      (itoa (cdr (assoc 62 e))) "\\t"
      (itoa (cdr (assoc 70 e))) "\\t"
      (cdr (assoc 6 e)))
    f)
  (setq e (tblnext "LAYER"))
)
"""


def _as_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_point(value: str) -> list[float] | None:
    parts = value.split(",")
    if len(parts) != 3:
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        return None


def _first(records: list[tuple[str, str]], key: str) -> str | None:
    """The value for `key`, or None when the script never emitted it.

    None is deliberately not merged with a default. An AutoLISP call that is
    unavailable in the core engine aborts its own line and leaves the rest of
    the script running, so a missing record means "could not be read" - and
    defaulting that to 0 or "" would report a confident wrong answer.
    """
    for name, value in records:
        if name == key:
            return value
    return None


def _require(records: list[tuple[str, str]], keys: tuple[str, ...]) -> dict[str, str]:
    """Fetch keys that the script is contracted to emit, or fail loudly."""
    found = {key: _first(records, key) for key in keys}
    missing = sorted(key for key, value in found.items() if value is None)
    if missing:
        raise autocad.EngineError(
            "E_SERVER",
            "the drawing probe did not report every required value",
            missing=missing,
            reported=sorted({name for name, _ in records}),
        )
    return {key: value for key, value in found.items() if value is not None}


def info(drawing: Path, timeout: float | None = None) -> dict[str, Any]:
    """Identity, units, extents, layouts and object counts for one drawing."""
    records = autocad.run_script(_INFO_BODY, drawing=drawing, readonly=True, timeout=timeout)
    values = _require(
        records,
        (
            "acadver",
            "dwgname",
            "dwgprefix",
            "insunits",
            "measurement",
            "extmin",
            "extmax",
            "layers",
            "blocks",
        ),
    )
    insunits = _as_int(values["insunits"])
    # Model is always present in the layout dictionary; paper-space layouts are
    # the rest, and an agent asking "how many sheets" means those.
    layout_names = [value for key, value in records if key == "layout"]
    paper_layouts = [name for name in layout_names if name.lower() != "model"]
    return {
        "path": str(drawing),
        "name": values["dwgname"],
        "directory": values["dwgprefix"],
        "format_version": values["acadver"],
        "units": {
            "insunits": insunits,
            "name": INSUNITS.get(insunits, f"unknown-{insunits}"),
            # MEASUREMENT picks the imperial or metric hatch/linetype file.
            "measurement": "metric" if _as_int(values["measurement"]) == 1 else "imperial",
        },
        "counts": {
            "layers": _as_int(values["layers"]),
            "layouts": len(paper_layouts),
            "blocks": _as_int(values["blocks"]),
        },
        "layouts": paper_layouts,
        "extents": {
            "min": _as_point(values["extmin"]),
            "max": _as_point(values["extmax"]),
        },
        # These strings come out of the file, not from the caller.
        "_untrusted": ["name", "directory", "layouts"],
    }


def layers(drawing: Path, limit: int | None = None, timeout: float | None = None) -> dict[str, Any]:
    """The layer table, in table order.

    A negative colour number is how a DWG stores "layer is off", which is
    distinct from frozen - an agent that conflates them will misread a drawing.
    """
    records = autocad.run_script(_LAYER_BODY, drawing=drawing, readonly=True, timeout=timeout)

    parsed: list[dict[str, Any]] = []
    for key, value in records:
        if key != "layer":
            continue
        fields = value.split("\t")
        name = fields[0] if fields else ""
        colour = _as_int(fields[1]) if len(fields) > 1 else 7
        flags = _as_int(fields[2]) if len(fields) > 2 else 0
        linetype = fields[3] if len(fields) > 3 else ""
        parsed.append(
            {
                "name": name,
                "color": abs(colour),
                "on": colour >= 0,
                "frozen": bool(flags & _FROZEN_BIT),
                "locked": bool(flags & _LOCKED_BIT),
                "linetype": linetype,
            }
        )

    total = len(parsed)
    truncated = limit is not None and 0 <= limit < total
    if truncated:
        parsed = parsed[:limit]

    data: dict[str, Any] = {
        "layers": parsed,
        "count": len(parsed),
        "total": total,
        "_untrusted": ["layers"],
    }
    if truncated:
        # Never hand back a short list that looks complete (CLI-SPEC section 8).
        data["truncated"] = True
    return data


# DXF group 70 on a BLOCK table record.
_ANONYMOUS_BIT = 1
_HAS_ATTRIBUTES_BIT = 2
_XREF_BIT = 4
_XREF_OVERLAY_BIT = 8


def entities(drawing: Path, timeout: float | None = None) -> dict[str, Any]:
    """How many objects of each DXF type the drawing holds.

    Walks `entnext`, which covers model and paper space but not the contents of
    block *definitions* - so a block placed once counts as one INSERT, not as
    its constituent geometry. `reference` says so; an agent sizing a drawing
    needs to know which of the two it is being told.
    """
    records = autocad.run_script(_ENTITY_BODY, drawing=drawing, readonly=True, timeout=timeout)
    by_type: dict[str, int] = {}
    for key, value in records:
        if key != "entity":
            continue
        name, _, count = value.partition("\t")
        by_type[name] = _as_int(count)

    scanned = _first(records, "scanned")
    if scanned is None:
        raise autocad.EngineError(
            "E_SERVER",
            "the entity scan did not report a total",
            reported=sorted({name for name, _ in records}),
        )
    return {
        "by_type": dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
        "distinct_types": len(by_type),
        "total": _as_int(scanned),
        "scope": "model and paper space; block definition contents are not expanded",
    }


def blocks(drawing: Path, limit: int | None = None, timeout: float | None = None) -> dict[str, Any]:
    """Block definitions with how many times each is actually placed.

    A definition with zero insertions is not an error - it is an unused block,
    which is exactly what someone auditing a drawing is looking for.
    """
    records = autocad.run_script(_BLOCK_BODY, drawing=drawing, readonly=True, timeout=timeout)

    parsed: list[dict[str, Any]] = []
    for key, value in records:
        if key != "block":
            continue
        fields = value.split("\t")
        name = fields[0] if fields else ""
        flags = _as_int(fields[1]) if len(fields) > 1 else 0
        inserts = _as_int(fields[2]) if len(fields) > 2 else 0
        path = fields[3] if len(fields) > 3 else ""
        parsed.append(
            {
                "name": name,
                "inserts": inserts,
                "anonymous": bool(flags & _ANONYMOUS_BIT),
                "has_attributes": bool(flags & _HAS_ATTRIBUTES_BIT),
                "xref": bool(flags & (_XREF_BIT | _XREF_OVERLAY_BIT)),
                "xref_path": path or None,
            }
        )

    total = len(parsed)
    truncated = limit is not None and 0 <= limit < total
    if truncated:
        parsed = parsed[:limit]

    data: dict[str, Any] = {
        "blocks": parsed,
        "count": len(parsed),
        "total": total,
        "xrefs": sum(1 for b in parsed if b["xref"]),
        "_untrusted": ["blocks"],
    }
    if truncated:
        data["truncated"] = True
    return data


def _lisp_string(value: str) -> str:
    """Escape a value for an AutoLISP string literal.

    AutoCAD forbids quotes and backslashes in layer names, so this is belt and
    braces - but a name arriving from somewhere else must not be able to close
    the literal and inject code into a script that is about to modify a file.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _layer_apply_body(plan: list[tuple[str, int, int]]) -> str:
    """Modify several layer records in one engine run, then save once.

    Batching matters here beyond tidiness: each accoreconsole start costs about
    three seconds, so a per-layer loop would turn a twenty-layer change into a
    minute of process launches.

    `(command "_.QSAVE")` is the single deliberate exception to this module's
    no-`command` rule: there is no other way to save from the core engine.
    ActiveX is not an option - `vl-load-com` succeeds but
    `(vlax-get-acad-object)` returns nil headless, so `vla-save` reports success
    and changes nothing. That silent no-op is exactly why the write path
    re-opens the file afterwards and verifies rather than trusting this script.
    """
    blocks = []
    for name, color, flags in plan:
        literal = _lisp_string(name)
        blocks.append(
            f'(setq e (tblobjname "LAYER" "{literal}"))\n'
            "(if e\n"
            "  (progn\n"
            "    (setq d (entget e))\n"
            f"    (setq d (subst (cons 62 {color}) (assoc 62 d) d))\n"
            f"    (setq d (subst (cons 70 {flags}) (assoc 70 d) d))\n"
            "    (if (entmod d)\n"
            f'      (write-line "applied|{literal}\\tyes" f)\n'
            f'      (write-line "applied|{literal}\\tno" f)))\n'
            f'  (write-line "applied|{literal}\\tabsent" f))'
        )
    return "\n".join([*blocks, '(command "_.QSAVE")', '(write-line "saved|yes" f)', ""])


def _as_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# DXF 75 on AcDbPlotSettings: 0 means "scaled to fit", anything else is a
# standard scale, in which case 142/143 carry the real ratio.
_SCALE_FIT = 0


def layouts(drawing: Path, timeout: float | None = None) -> dict[str, Any]:
    """Paper-space layouts with their sheet size and plot scale.

    `Model` is included and flagged rather than filtered out: an agent asking
    "what sheets are in here" needs to see that the model tab was counted and
    excluded, not silently wonder where it went.
    """
    records = autocad.run_script(_LAYOUT_BODY, drawing=drawing, readonly=True, timeout=timeout)

    parsed: list[dict[str, Any]] = []
    for key, value in records:
        if key != "layout":
            continue
        fields = value.split("\t")
        if len(fields) < 9:
            continue
        name, tab, device, media, width, height, numerator, denominator, scale_type = fields[:9]
        fit = _as_int(scale_type) == _SCALE_FIT
        parsed.append(
            {
                "name": name,
                "is_model": name.lower() == "model",
                "tab_order": _as_int(tab),
                "paper": {
                    "media": media or None,
                    # AcDbPlotSettings always stores the sheet in millimetres.
                    "width_mm": _as_float(width),
                    "height_mm": _as_float(height),
                },
                "plot": {
                    "device": device or None,
                    "fit_to_paper": fit,
                    "scale_numerator": None if fit else _as_float(numerator),
                    "scale_denominator": None if fit else _as_float(denominator),
                },
            }
        )

    parsed.sort(key=lambda item: item["tab_order"])
    return {
        "layouts": parsed,
        "count": len(parsed),
        "paper_space_count": sum(1 for item in parsed if not item["is_model"]),
        "_untrusted": ["layouts"],
    }


# DXF 70 on a BLOCK record, xref-specific bits.
_XREF_RESOLVED_BIT = 32


def xrefs(drawing: Path, timeout: float | None = None) -> dict[str, Any]:
    """External references, and whether each one's file is actually there.

    A broken xref is the most common real defect in a delivered drawing set, and
    it is invisible from inside the DWG alone - the block record happily names a
    path that no longer exists. Relative paths are resolved against the host
    drawing's own directory, which is how AutoCAD resolves them.
    """
    records = autocad.run_script(_XREF_BODY, drawing=drawing, readonly=True, timeout=timeout)
    host_directory = drawing.parent

    parsed: list[dict[str, Any]] = []
    for key, value in records:
        if key != "xref":
            continue
        fields = value.split("\t", 2)
        if len(fields) < 3:
            continue
        name, flags_text, path = fields
        flags = _as_int(flags_text)
        # The stored path is always Windows-shaped, because AutoCAD wrote it.
        # Parsing it with the host's own flavour would treat `.\Res\child.dwg`
        # as one long filename anywhere but Windows.
        stored = PureWindowsPath(path)
        if stored.is_absolute():
            candidate = Path(str(stored))
        else:
            candidate = host_directory.joinpath(*stored.parts)
        found = candidate.is_file()
        parsed.append(
            {
                "name": name,
                "path": path,
                "resolved_path": str(candidate) if found else None,
                "overlay": bool(flags & _XREF_OVERLAY_BIT),
                "loaded": bool(flags & _XREF_RESOLVED_BIT),
                "file_found": found,
            }
        )

    missing = [item["name"] for item in parsed if not item["file_found"]]
    return {
        "xrefs": parsed,
        "count": len(parsed),
        "missing_count": len(missing),
        "missing": missing,
        "_untrusted": ["xrefs", "missing"],
    }


def text(drawing: Path, limit: int | None = None, timeout: float | None = None) -> dict[str, Any]:
    """Every TEXT, MTEXT and ATTDEF string, with its layer and insertion point.

    This is the most attacker-reachable payload the tool returns: an agent
    reading a title block is reading whatever the drawing's author typed. It is
    reported under `_untrusted` and must never be treated as instruction.

    MTEXT content still carries its inline formatting codes (`\\P`, `{\\f...}`);
    stripping them would be lossy guesswork, so the raw string is returned.
    """
    records = autocad.run_script(_TEXT_BODY, drawing=drawing, readonly=True, timeout=timeout)

    parsed: list[dict[str, Any]] = []
    for key, value in records:
        if key != "text":
            continue
        # Content is last and may itself contain tabs, so split only the head.
        fields = value.split("\t", 3)
        if len(fields) < 4:
            continue
        kind, layer, position, content = fields
        parsed.append(
            {
                "type": kind,
                "layer": layer,
                "position": _as_point(position + ",0") if position.count(",") == 1 else None,
                "content": content,
            }
        )

    total = len(parsed)
    truncated = limit is not None and 0 <= limit < total
    if truncated:
        parsed = parsed[:limit]

    data: dict[str, Any] = {
        "items": parsed,
        "count": len(parsed),
        "total": total,
        "_untrusted": ["items"],
    }
    if truncated:
        data["truncated"] = True
    return data


def _resolve_targets(names: list[str]) -> list[str]:
    """De-duplicate while preserving the caller's order.

    Input order is what an agent maps results back onto, so `items[]` must come
    back in it (CLI-SPEC section 15.1).
    """
    if not names:
        raise autocad.EngineError(
            "E_VALIDATION", "no layers named: --names takes at least one layer"
        )
    return list(dict.fromkeys(names))


def _desired(current: dict[str, Any], **wanted: Any) -> dict[str, Any]:
    """Merge the requested changes onto the observed state."""
    target = {key: current[key] for key in ("color", "on", "frozen", "locked")}
    for key, value in wanted.items():
        if value is not None:
            target[key] = value
    return target


def _encode(state: dict[str, Any]) -> tuple[int, int]:
    """Back to the DXF pair: 62 signed for on/off, 70 bit-packed."""
    color = abs(int(state["color"])) or 7
    if not state["on"]:
        color = -color
    flags = (_FROZEN_BIT if state["frozen"] else 0) | (_LOCKED_BIT if state["locked"] else 0)
    return color, flags


def _batch_result(
    items: list[dict[str, Any]], skipped: list[str], backup_path: Path | None
) -> dict[str, Any]:
    """The shape every batch write answers in (CLI-SPEC section 15.5).

    Shared so `layer set`, `layer create` and `layer delete` cannot drift into
    three slightly different notions of what a summary is.
    """
    succeeded = sum(1 for entry in items if entry["ok"])
    summary = {"total": len(items), "succeeded": succeeded, "failed": len(items) - succeeded}
    if skipped:
        summary["skipped"] = len(skipped)
    return {
        "items": items,
        "summary": summary,
        "verification": {"level": "reopened-and-compared", "matches": summary["failed"] == 0},
        "backup": str(backup_path) if backup_path else None,
        "backup_note": (
            "byte copy of the file as it was on disk; unsaved editor state is not covered"
        ),
        "_untrusted": ["items"],
    }


def set_layers(
    drawing: Path,
    names: list[str],
    *,
    color: int | None = None,
    on: bool | None = None,
    frozen: bool | None = None,
    locked: bool | None = None,
    dry_run: bool = False,
    confirm_token: str | None = None,
    continue_on_error: bool = True,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Change colour or on/frozen/locked state on one or more layers.

    One command, one envelope, one confirm token and one aggregated result no
    matter how many layers are named (CLI-SPEC section 15). A single layer is a
    batch of one and comes back in the same shape, so an agent never has to
    branch on how many it asked for.
    """
    # Fail closed before doing any work, so an agent learns it cannot write at
    # the moment it plans the write rather than after building on the preview.
    confirm.require_write_permission()

    requested = {"color": color, "on": on, "frozen": frozen, "locked": locked}
    if all(value is None for value in requested.values()):
        raise autocad.EngineError(
            "E_VALIDATION",
            "nothing to change: pass at least one of --color/--on/--off/"
            "--freeze/--thaw/--lock/--unlock",
        )
    if color is not None and not 1 <= color <= 255:
        raise autocad.EngineError(
            "E_VALIDATION", "--color must be an AutoCAD Color Index in 1..255", color=color
        )

    targets = _resolve_targets(names)
    table = {layer["name"]: layer for layer in layers(drawing, timeout=timeout)["layers"]}

    # Resolve every target up front. A name that is not in the drawing becomes
    # its own failed item rather than sinking the batch: hiding nineteen good
    # results behind one typo would be the worse failure.
    plan: list[dict[str, Any]] = []
    for name in targets:
        current = table.get(name)
        if current is None:
            plan.append({"target": name, "missing": True})
            continue
        wanted = _desired(current, **requested)
        plan.append(
            {
                "target": name,
                "missing": False,
                "current": current,
                "wanted": wanted,
                "delta": {k: v for k, v in wanted.items() if current[k] != v},
            }
        )

    applicable = [item for item in plan if not item["missing"]]
    scope = confirm.scope(
        command="layer set",
        target=drawing,
        arguments={
            "names": targets,
            **{k: v for k, v in requested.items() if v is not None},
            "continue_on_error": continue_on_error,
        },
        # Binds the whole resolved set: adding or removing a target, or any of
        # them moving underneath us, voids the token (CLI-SPEC section 15.2).
        observed={item["target"]: item.get("current") for item in plan},
    )

    if dry_run:
        token = confirm.mint(scope)
        return {
            "preview": {
                "action": "modify",
                "resource": "layer",
                "total": len(targets),
                "targets": targets,
                "changes": [
                    {
                        "action": "modify",
                        "resource": "layer",
                        "id": item["target"],
                        "before": {k: item["current"][k] for k in item["delta"]},
                        "after": dict(item["delta"]),
                    }
                    for item in applicable
                    if item["delta"]
                ],
                "unchanged": [item["target"] for item in applicable if not item["delta"]],
                "unresolved": [item["target"] for item in plan if item["missing"]],
            },
            "confirm_token": token.value,
            "expires_at": token.expires_at,
            "_untrusted": ["preview"],
        }

    if confirm_token is None:
        raise autocad.EngineError(
            "E_CONFIRMATION_REQUIRED",
            "run the same command with --dry-run to obtain a confirm token",
        )

    # Spends the token; any mismatch with the scope raises E_CONFLICT.
    confirm.verify_and_consume(confirm_token, scope)

    # `--continue-on-error false` stops at the first target that cannot be
    # applied. Anything past it is reported as skipped so the agent can resume
    # instead of guessing how far the batch got (CLI-SPEC section 15.5).
    attempt, skipped = applicable, []
    if not continue_on_error:
        stop_at = next((index for index, item in enumerate(plan) if item["missing"]), None)
        if stop_at is not None:
            attempt = [item for item in plan[:stop_at] if not item["missing"]]
            skipped = [item["target"] for item in plan[stop_at + 1 :]]

    backup_path = confirm.backup(drawing) if attempt else None
    outcomes: dict[str, str] = {}
    if attempt:
        records = autocad.run_script(
            _layer_apply_body([(item["target"], *_encode(item["wanted"])) for item in attempt]),
            drawing=drawing,
            readonly=False,
            timeout=timeout,
        )
        for key, value in records:
            if key != "applied":
                continue
            name, _, outcome = value.partition("\t")
            outcomes[name] = outcome

    # Re-open the file from disk in a fresh engine run. The in-memory result of
    # the write cannot vouch for what actually landed.
    verified = {layer["name"]: layer for layer in layers(drawing, timeout=timeout)["layers"]}

    items: list[dict[str, Any]] = []
    for item in plan:
        name = item["target"]
        if item["missing"]:
            items.append(
                {"target": name, "ok": False, "error": {"code": "E_NOT_FOUND", "retryable": False}}
            )
            continue
        if name in skipped or name not in outcomes:
            items.append(
                {
                    "target": name,
                    "ok": False,
                    "skipped": True,
                    "error": {"code": "E_CONFLICT", "retryable": True},
                }
            )
            continue
        observed = verified.get(name)
        matched = observed is not None and all(
            observed[key] == value for key, value in item["wanted"].items()
        )
        entry: dict[str, Any] = {
            "target": name,
            "ok": outcomes[name] == "yes" and matched,
            "changed": bool(item["delta"]),
            "after": observed,
        }
        if not entry["ok"]:
            entry["error"] = {"code": "E_SERVER", "retryable": True}
        items.append(entry)

    return _batch_result(items, skipped, backup_path)


# Which layers hold objects, and which one is current. Both block a delete, so
# the dry-run can say *why* a target will survive instead of finding out after.
_LAYER_USAGE_BODY = """
(setq counts '())
(setq e (entnext))
(while e
  (setq ly (cdr (assoc 8 (entget e))))
  (setq hit (assoc ly counts))
  (if hit
    (setq counts (subst (cons ly (1+ (cdr hit))) hit counts))
    (setq counts (cons (cons ly 1) counts)))
  (setq e (entnext e)))
(foreach c counts
  (write-line (strcat "usage|" (car c) "\\t" (itoa (cdr c))) f))
(write-line (strcat "clayer|" (getvar "CLAYER")) f)
"""

# Layer "0" is structural; AutoCAD refuses to delete it and so do we, earlier
# and with a better message.
_PROTECTED_LAYERS = frozenset({"0"})


def _layer_create_body(plan: list[tuple[str, int, int, str]]) -> str:
    """`entmake` a layer table record per target, then save.

    Creation is the one layer operation with a real return value: `entmake`
    answers nil on failure, so the per-item outcome here is genuine rather than
    inferred. Deletion has no such signal (see `_layer_delete_body`).
    """
    blocks = []
    for name, color, flags, linetype in plan:
        literal = _lisp_string(name)
        blocks.append(
            '(if (entmake (list \'(0 . "LAYER") \'(100 . "AcDbSymbolTableRecord")\n'
            '                   \'(100 . "AcDbLayerTableRecord")\n'
            f'                   (cons 2 "{literal}") (cons 70 {flags})\n'
            f'                   (cons 62 {color}) (cons 6 "{_lisp_string(linetype)}")))\n'
            f'  (write-line "created|{literal}\\tyes" f)\n'
            f'  (write-line "created|{literal}\\tno" f))'
        )
    return "\n".join([*blocks, '(command "_.QSAVE")', ""])


def _layer_delete_body(names: list[str]) -> str:
    """Delete layer records through `-LAYER`, then save.

    This is the second deliberate exception to the no-`command` rule, and it
    needed proving before it could be trusted: the refusal paths are where a
    prompt-driven command would hang. Deleting layer 0, the current layer, a
    layer holding objects and a layer that does not exist were each measured on
    a real drawing - all four return control in under four seconds.

    What they do *not* do is report which of those happened. The command's
    result is identical whether it deleted or refused, so the caller must
    re-read the layer table to learn the truth.
    """
    lines = []
    for name in names:
        literal = _lisp_string(name)
        lines.append(f'(command "_.-LAYER" "_Delete" "{literal}" "")')
    return "\n".join([*lines, '(command "_.QSAVE")', '(write-line "attempted|yes" f)', ""])


def _layer_usage(drawing: Path, timeout: float | None) -> tuple[dict[str, int], str]:
    records = autocad.run_script(_LAYER_USAGE_BODY, drawing=drawing, readonly=True, timeout=timeout)
    usage: dict[str, int] = {}
    for key, value in records:
        if key != "usage":
            continue
        name, _, count = value.partition("\t")
        usage[name] = _as_int(count)
    current = _first(records, "clayer")
    if current is None:
        raise autocad.EngineError(
            "E_SERVER",
            "the layer usage probe did not report the current layer",
            reported=sorted({name for name, _ in records}),
        )
    return usage, current


def create_layers(
    drawing: Path,
    names: list[str],
    *,
    color: int = 7,
    linetype: str = "Continuous",
    dry_run: bool = False,
    confirm_token: str | None = None,
    continue_on_error: bool = True,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Create one or more layers.

    Additive and reversible, so it carries no `--dangerous` gate - but it is
    still a write, and goes through permission, preview, token and read-back
    like every other.
    """
    confirm.require_write_permission()
    if not 1 <= color <= 255:
        raise autocad.EngineError(
            "E_VALIDATION", "--color must be an AutoCAD Color Index in 1..255", color=color
        )

    targets = _resolve_targets(names)
    existing = {layer["name"] for layer in layers(drawing, timeout=timeout)["layers"]}

    # Creating a layer that is already there is reported as a conflict rather
    # than a quiet success: the caller asked for a layer with these properties,
    # and the one that exists may have entirely different ones.
    plan = [{"target": name, "exists": name in existing} for name in targets]
    fresh = [item for item in plan if not item["exists"]]

    scope = confirm.scope(
        command="layer create",
        target=drawing,
        arguments={
            "names": targets,
            "color": color,
            "linetype": linetype,
            "continue_on_error": continue_on_error,
        },
        observed={"existing": sorted(existing & set(targets))},
    )

    if dry_run:
        token = confirm.mint(scope)
        return {
            "preview": {
                "action": "create",
                "resource": "layer",
                "total": len(targets),
                "targets": targets,
                "changes": [
                    {
                        "action": "create",
                        "resource": "layer",
                        "id": item["target"],
                        "before": None,
                        "after": {"color": color, "linetype": linetype},
                    }
                    for item in fresh
                ],
                "already_present": [item["target"] for item in plan if item["exists"]],
            },
            "confirm_token": token.value,
            "expires_at": token.expires_at,
            "_untrusted": ["preview"],
        }

    if confirm_token is None:
        raise autocad.EngineError(
            "E_CONFIRMATION_REQUIRED",
            "run the same command with --dry-run to obtain a confirm token",
        )
    confirm.verify_and_consume(confirm_token, scope)

    attempt, skipped = fresh, []
    if not continue_on_error:
        stop_at = next((index for index, item in enumerate(plan) if item["exists"]), None)
        if stop_at is not None:
            attempt = [item for item in plan[:stop_at] if not item["exists"]]
            skipped = [item["target"] for item in plan[stop_at + 1 :]]

    backup_path = confirm.backup(drawing) if attempt else None
    outcomes: dict[str, str] = {}
    if attempt:
        records = autocad.run_script(
            _layer_create_body([(item["target"], color, 0, linetype) for item in attempt]),
            drawing=drawing,
            readonly=False,
            timeout=timeout,
        )
        for key, value in records:
            if key != "created":
                continue
            name, _, outcome = value.partition("\t")
            outcomes[name] = outcome

    verified = {layer["name"]: layer for layer in layers(drawing, timeout=timeout)["layers"]}

    items: list[dict[str, Any]] = []
    for item in plan:
        name = item["target"]
        if item["exists"]:
            items.append(
                {
                    "target": name,
                    "ok": False,
                    "error": {"code": "E_CONFLICT", "retryable": False},
                    "reason": "a layer with that name already exists",
                }
            )
            continue
        if name in skipped or name not in outcomes:
            items.append(
                {
                    "target": name,
                    "ok": False,
                    "skipped": True,
                    "error": {"code": "E_CONFLICT", "retryable": True},
                }
            )
            continue
        present = verified.get(name)
        entry: dict[str, Any] = {
            "target": name,
            "ok": outcomes[name] == "yes" and present is not None,
            "after": present,
        }
        if not entry["ok"]:
            entry["error"] = {"code": "E_SERVER", "retryable": True}
        items.append(entry)

    return _batch_result(items, skipped, backup_path)


def delete_layers(
    drawing: Path,
    names: list[str],
    *,
    dangerous: bool = False,
    dry_run: bool = False,
    confirm_token: str | None = None,
    continue_on_error: bool = True,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Delete one or more layers. Irreversible inside the file.

    Two independent gates, per CLI-SPEC section 15.4: `--dangerous` declares the
    intent, and the confirm token authorises the specific resolved set. Neither
    alone will do anything - a valid token without `--dangerous` is still
    refused.

    The preview says which targets will survive and why, because AutoCAD refuses
    to delete layer 0, the current layer, or a layer holding objects - and its
    command reports refusal and success identically. The pre-check is advisory
    (it does not see inside block definitions); the read-back afterwards is what
    decides each item's outcome.
    """
    confirm.require_write_permission()

    targets = _resolve_targets(names)
    existing = {layer["name"] for layer in layers(drawing, timeout=timeout)["layers"]}
    usage, current = _layer_usage(drawing, timeout)

    def blocker(name: str) -> str | None:
        if name not in existing:
            return "no such layer"
        if name in _PROTECTED_LAYERS:
            return "layer 0 cannot be deleted"
        if name == current:
            return "layer is the current layer"
        if usage.get(name):
            return f"layer holds {usage[name]} object(s)"
        return None

    plan = [{"target": name, "blocked": blocker(name)} for name in targets]
    removable = [item for item in plan if item["blocked"] is None]

    scope = confirm.scope(
        command="layer delete",
        target=drawing,
        arguments={"names": targets, "continue_on_error": continue_on_error},
        observed={item["target"]: item["blocked"] for item in plan},
    )

    if dry_run:
        token = confirm.mint(scope)
        return {
            "preview": {
                "action": "delete",
                "resource": "layer",
                "total": len(targets),
                "targets": targets,
                "changes": [
                    {
                        "action": "delete",
                        "resource": "layer",
                        "id": item["target"],
                        "before": {"name": item["target"]},
                        "after": None,
                    }
                    for item in removable
                ],
                "blocked": {item["target"]: item["blocked"] for item in plan if item["blocked"]},
                "requires": "--dangerous",
            },
            "confirm_token": token.value,
            "expires_at": token.expires_at,
            "_untrusted": ["preview"],
        }

    if confirm_token is None:
        raise autocad.EngineError(
            "E_CONFIRMATION_REQUIRED",
            "run the same command with --dry-run to obtain a confirm token",
        )
    # Checked after the token so a caller cannot learn anything by omitting it,
    # and before consuming the token so a forgotten flag does not burn one.
    if not dangerous:
        raise autocad.EngineError(
            "E_CONFIRMATION_REQUIRED",
            "deleting layers is irreversible: pass --dangerous as well as --confirm",
            requires="--dangerous",
        )
    confirm.verify_and_consume(confirm_token, scope)

    attempt, skipped = removable, []
    if not continue_on_error:
        stop_at = next((index for index, item in enumerate(plan) if item["blocked"]), None)
        if stop_at is not None:
            attempt = [item for item in plan[:stop_at] if item["blocked"] is None]
            skipped = [item["target"] for item in plan[stop_at + 1 :]]

    backup_path = confirm.backup(drawing) if attempt else None
    if attempt:
        autocad.run_script(
            _layer_delete_body([item["target"] for item in attempt]),
            drawing=drawing,
            readonly=False,
            timeout=timeout,
        )

    # The only trustworthy signal: is it gone from the reopened file?
    remaining = {layer["name"] for layer in layers(drawing, timeout=timeout)["layers"]}

    items: list[dict[str, Any]] = []
    for item in plan:
        name = item["target"]
        if item["blocked"]:
            items.append(
                {
                    "target": name,
                    "ok": False,
                    "error": {
                        "code": "E_NOT_FOUND"
                        if item["blocked"] == "no such layer"
                        else "E_CONFLICT",
                        "retryable": False,
                    },
                    "reason": item["blocked"],
                }
            )
            continue
        if name in skipped:
            items.append(
                {
                    "target": name,
                    "ok": False,
                    "skipped": True,
                    "error": {"code": "E_CONFLICT", "retryable": True},
                }
            )
            continue
        gone = name not in remaining
        entry: dict[str, Any] = {"target": name, "ok": gone, "deleted": gone}
        if not gone:
            entry["error"] = {"code": "E_CONFLICT", "retryable": False}
            entry["reason"] = "AutoCAD refused the delete; the layer is still referenced"
        items.append(entry)

    return _batch_result(items, skipped, backup_path)


# Geometry is created with `entmake`, which answers nil on failure - so unlike
# `-LAYER Delete` these writes have a genuine per-item signal. The read-back
# still runs: a signal from the write script says the object was accepted into
# the in-memory database, not that the file on disk now holds it.
_DRAW_SHAPES = ("line", "circle")


def _draw_body(layer: str, shapes: list[tuple[str, tuple[float, ...]]]) -> str:
    literal = _lisp_string(layer)
    lines = []
    for index, (kind, values) in enumerate(shapes):
        if kind == "line":
            x1, y1, x2, y2 = values
            entity = (
                f'(list \'(0 . "LINE") (cons 8 "{literal}")'
                f" (list 10 {x1} {y1} 0.0) (list 11 {x2} {y2} 0.0))"
            )
        else:
            cx, cy, radius = values
            entity = (
                f'(list \'(0 . "CIRCLE") (cons 8 "{literal}")'
                f" (list 10 {cx} {cy} 0.0) (cons 40 {radius}))"
            )
        lines.append(
            f"(if (entmake {entity})\n"
            f'  (write-line "drawn|{index}\\tyes" f)\n'
            f'  (write-line "drawn|{index}\\tno" f))'
        )
    return "\n".join([*lines, '(command "_.QSAVE")', ""])


def _parse_tuple(raw: str, arity: int, label: str) -> tuple[float, ...]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != arity:
        raise autocad.EngineError(
            "E_VALIDATION",
            f"{label} needs {arity} comma-separated numbers",
            got=raw,
        )
    try:
        return tuple(float(part) for part in parts)
    except ValueError:
        raise autocad.EngineError("E_VALIDATION", f"{label} must be numbers", got=raw) from None


def parse_shapes(kind: str, raw_values: list[str]) -> list[tuple[str, tuple[float, ...]]]:
    """Turn the repeated flag values into validated shapes.

    Validation happens here rather than in AutoLISP because a malformed number
    reaching the engine is a silent no-op at best; a clear E_VALIDATION before
    anything is written is strictly better for the caller.
    """
    if kind == "line":
        shapes = [("line", _parse_tuple(value, 4, "--segments")) for value in raw_values]
        for _, (x1, y1, x2, y2) in shapes:
            if (x1, y1) == (x2, y2):
                raise autocad.EngineError(
                    "E_VALIDATION",
                    "a segment needs two different endpoints",
                    got=f"{x1},{y1},{x2},{y2}",
                )
        return shapes
    shapes = [("circle", _parse_tuple(value, 3, "--circles")) for value in raw_values]
    for _, (_, _, radius) in shapes:
        if radius <= 0:
            raise autocad.EngineError(
                "E_VALIDATION", "a circle needs a positive radius", got=radius
            )
    return shapes


def draw(
    drawing: Path,
    kind: str,
    shapes: list[tuple[str, tuple[float, ...]]],
    *,
    layer: str,
    dry_run: bool = False,
    confirm_token: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Add geometry to an existing layer.

    Additive, so no `--dangerous` gate - but it is the first command that puts
    new objects into a drawing, and it goes through the same permission,
    preview, single-use token, backup and read-back as every other write.

    Verification counts objects of this DXF type before and after and checks the
    delta. That is weaker than identifying each new object, and the result says
    so rather than implying more than it checked.
    """
    confirm.require_write_permission()
    if kind not in _DRAW_SHAPES:
        raise autocad.EngineError(
            "E_VALIDATION", f"unknown shape {kind!r}", supported=list(_DRAW_SHAPES)
        )
    if not shapes:
        raise autocad.EngineError("E_VALIDATION", "nothing to draw: no shapes given")

    # entmake will happily accept an undefined layer name and leave the objects
    # somewhere the caller did not intend, so the layer is checked first.
    known = {entry["name"] for entry in layers(drawing, timeout=timeout)["layers"]}
    if layer not in known:
        raise autocad.EngineError(
            "E_NOT_FOUND",
            f"the drawing has no layer named {layer!r}; create it first",
            layer=layer,
            available=sorted(known)[:40],
        )

    dxf_type = "LINE" if kind == "line" else "CIRCLE"
    before = entities(drawing, timeout=timeout)["by_type"].get(dxf_type, 0)

    scope = confirm.scope(
        command=f"draw {kind}",
        target=drawing,
        arguments={
            "layer": layer,
            "shapes": [list(values) for _, values in shapes],
        },
        observed={"existing_of_type": before, "dxf_type": dxf_type},
    )

    if dry_run:
        token = confirm.mint(scope)
        return {
            "preview": {
                "action": "create",
                "resource": dxf_type.lower(),
                "layer": layer,
                "total": len(shapes),
                "changes": [
                    {
                        "action": "create",
                        "resource": dxf_type.lower(),
                        "id": f"#{index}",
                        "before": None,
                        "after": dict(zip(_SHAPE_FIELDS[kind], values, strict=True)),
                    }
                    for index, (_, values) in enumerate(shapes)
                ],
            },
            "confirm_token": token.value,
            "expires_at": token.expires_at,
            "_untrusted": ["preview"],
        }

    if confirm_token is None:
        raise autocad.EngineError(
            "E_CONFIRMATION_REQUIRED",
            "run the same command with --dry-run to obtain a confirm token",
        )
    confirm.verify_and_consume(confirm_token, scope)

    backup_path = confirm.backup(drawing)
    records = autocad.run_script(
        _draw_body(layer, shapes), drawing=drawing, readonly=False, timeout=timeout
    )
    outcomes: dict[str, str] = {}
    for key, value in records:
        if key != "drawn":
            continue
        index, _, outcome = value.partition("\t")
        outcomes[index] = outcome

    after = entities(drawing, timeout=timeout)["by_type"].get(dxf_type, 0)
    accepted = sum(1 for outcome in outcomes.values() if outcome == "yes")

    items = [
        {
            "target": f"#{index}",
            "ok": outcomes.get(str(index)) == "yes",
            "shape": dict(zip(_SHAPE_FIELDS[kind], values, strict=True)),
            **(
                {}
                if outcomes.get(str(index)) == "yes"
                else {"error": {"code": "E_SERVER", "retryable": True}}
            ),
        }
        for index, (_, values) in enumerate(shapes)
    ]

    result = _batch_result(items, [], backup_path)
    result["layer"] = layer
    result["verification"] = {
        # Counting is weaker than identifying each object; say which was done.
        "level": "reopened-and-counted",
        "dxf_type": dxf_type,
        "before": before,
        "after": after,
        "expected_delta": accepted,
        "matches": after - before == accepted,
    }
    return result


_SHAPE_FIELDS = {
    "line": ("x1", "y1", "x2", "y2"),
    "circle": ("center_x", "center_y", "radius"),
}
