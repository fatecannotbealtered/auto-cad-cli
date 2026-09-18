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

from pathlib import Path
from typing import Any

from . import autocad

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
