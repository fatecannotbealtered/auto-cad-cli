"""Read-only drawing commands, served by the headless core engine.

Everything here opens the drawing with `/readonly` in a separate accoreconsole
process, so a command can never disturb a drawing the operator has open in the
GUI, and none of it needs an AutoCAD window at all.

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


def _layer_apply_body(name: str, color: int, flags: int) -> str:
    """Modify one layer record, then save.

    `(command "_.QSAVE")` is the single deliberate exception to this module's
    no-`command` rule: there is no other way to save from the core engine.
    ActiveX is not an option - `vl-load-com` succeeds but
    `(vlax-get-acad-object)` returns nil headless, so `vla-save` reports success
    and changes nothing. That silent no-op is exactly why the write path
    re-opens the file afterwards and verifies rather than trusting this script.
    """
    return f"""
(setq e (tblobjname "LAYER" "{_lisp_string(name)}"))
(if e
  (progn
    (setq d (entget e))
    (setq d (subst (cons 62 {color}) (assoc 62 d) d))
    (setq d (subst (cons 70 {flags}) (assoc 70 d) d))
    (if (entmod d)
      (write-line "applied|yes" f)
      (write-line "applied|no" f)))
  (write-line "applied|absent" f))
(command "_.QSAVE")
(write-line "saved|yes" f)
"""


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


def _find_layer(drawing: Path, name: str, timeout: float | None) -> dict[str, Any]:
    """Current state of one layer, or E_NOT_FOUND naming what is there."""
    table = layers(drawing, timeout=timeout)["layers"]
    for layer in table:
        if layer["name"] == name:
            return layer
    raise autocad.EngineError(
        "E_NOT_FOUND",
        f"the drawing has no layer named {name!r}",
        layer=name,
        available=[entry["name"] for entry in table][:40],
    )


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


def set_layer(
    drawing: Path,
    name: str,
    *,
    color: int | None = None,
    on: bool | None = None,
    frozen: bool | None = None,
    locked: bool | None = None,
    dry_run: bool = False,
    confirm_token: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Change one layer's colour or on/frozen/locked state.

    The smallest useful write in the tool: bounded to a single named object, and
    reversible by running the inverse command. Even so it goes through the full
    gate - permission, preview, single-use token, backup, and an independent
    read-back - because the machinery has to be right before anything larger
    uses it.
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

    current = _find_layer(drawing, name, timeout)
    target = _desired(current, **requested)
    changed = {k: (current[k], target[k]) for k in target if current[k] != target[k]}

    scope = confirm.scope(
        command="layer set",
        target=drawing,
        arguments={"name": name, **{k: v for k, v in requested.items() if v is not None}},
        observed=current,
    )
    preview = {
        "changes": [
            {
                "action": "modify",
                "resource": "layer",
                "id": name,
                "before": {k: current[k] for k in changed},
                "after": {k: target[k] for k in changed},
            }
        ]
        if changed
        else [],
        "no_op": not changed,
    }

    if dry_run:
        token = confirm.mint(scope)
        return {
            "preview": preview,
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

    backup_path = confirm.backup(drawing)
    color_code, flags = _encode(target)
    records = autocad.run_script(
        _layer_apply_body(name, color_code, flags),
        drawing=drawing,
        readonly=False,
        timeout=timeout,
    )
    applied = _first(records, "applied")

    # Re-open the file from disk in a fresh engine run. The in-memory result of
    # the write cannot vouch for what actually landed.
    observed = _find_layer(drawing, name, timeout)
    matches = all(observed[key] == value for key, value in target.items())

    return {
        "layer": name,
        "applied": applied == "yes",
        "changed": bool(changed),
        "before": current,
        "after": observed,
        "verification": {
            "level": "reopened-and-compared",
            "matches": matches,
            "intended": target,
        },
        "backup": str(backup_path) if backup_path else None,
        "backup_note": (
            "byte copy of the file as it was on disk; unsaved editor state is not covered"
        ),
        "_untrusted": ["layer", "before", "after"],
    }
