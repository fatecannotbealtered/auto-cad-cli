"""Discovery of the local AutoCAD install and the accoreconsole channel.

Two automation channels exist and this module owns the read-only one:

- **accoreconsole** (here): the headless core engine. It never touches a GUI
  session, accepts `/readonly`, and runs on a machine with no AutoCAD window
  open, so every read command goes through it.
- **COM** (probed here, not driven): attaches to a *running* editor. Only
  `probe_com` lives in this module; actually driving a live session is an
  opt-in write path that does not exist yet.

Four things about accoreconsole are load-bearing and were each found the hard
way on a real 2026 install:

1. Its stdout is **UTF-16LE**, not UTF-8.
2. Invoked without `/s <script>` it never exits - it silently creates a drawing
   from the default template and blocks on the `Command:` prompt forever.
3. It echoes every script line back, prefixed by a *localised* "Command:", so
   parsing stdout means parsing around translated text.
4. AutoLISP `princ` prints its result twice (the output, then the returned
   string literal).

(3) and (4) are why results come back through a side-channel file that the
script writes, rather than by scraping stdout. (2) is why every call is bounded
by a timeout. (1) still matters for surfacing engine errors.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Overrides discovery when AutoCAD lives somewhere the registry does not admit.
ENV_HOME = "AUTO_CAD_CLI_ACAD_HOME"
ENV_TIMEOUT = "AUTO_CAD_CLI_TIMEOUT"

DEFAULT_TIMEOUT = 180.0

# The script writes results here, one `key|value` record per line, and stdout is
# left to the engine's own chatter.
_BEGIN = "BEGIN_RECORDS"
_END = "END_RECORDS"


class EngineError(Exception):
    """A failure to obtain a result from the AutoCAD core engine.

    Carries the contract error code so command code never has to re-classify.
    """

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Install:
    release: str
    product: str
    location: Path
    language: str

    @property
    def accoreconsole(self) -> Path | None:
        candidate = self.location / "accoreconsole.exe"
        return candidate if candidate.is_file() else None

    @property
    def acad(self) -> Path | None:
        candidate = self.location / "acad.exe"
        return candidate if candidate.is_file() else None


def _registry_installs() -> list[Install]:
    """Read HKLM\\SOFTWARE\\Autodesk\\AutoCAD\\<release>\\<product>\\AcadLocation.

    The product key carries a locale suffix (`ACAD-9101:804`), which is how a
    Chinese install is distinguished from an English one at the same release.
    """
    try:
        import winreg
    except ImportError:  # not Windows
        return []

    installs: list[Install] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Autodesk\AutoCAD")
    except OSError:
        return []

    with root:
        for release in _subkeys(root):
            try:
                release_key = winreg.OpenKey(root, release)
            except OSError:
                continue
            with release_key:
                for product in _subkeys(release_key):
                    try:
                        product_key = winreg.OpenKey(release_key, product)
                    except OSError:
                        continue
                    with product_key:
                        location = _value(product_key, "AcadLocation")
                        if not location:
                            continue
                        installs.append(
                            Install(
                                release=release,
                                product=product,
                                location=Path(location),
                                language=_value(product_key, "Language") or "unknown",
                            )
                        )
    return installs


def _subkeys(key) -> list[str]:
    import winreg

    names, index = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def _value(key, name: str) -> str | None:
    import winreg

    try:
        data, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(data).strip().rstrip("\\") or None


def _release_sort_key(release: str) -> tuple:
    """`R25.1` sorts above `R24.3`; anything unparseable sorts last."""
    numbers = [int(n) for n in re.findall(r"\d+", release)]
    return (1, numbers) if numbers else (0, [])


def find_installs() -> list[Install]:
    """Every discoverable install, newest release first.

    An explicit `AUTO_CAD_CLI_ACAD_HOME` always wins and is returned alone: if
    the operator names a directory, silently preferring a different one found in
    the registry would be worse than failing.
    """
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        location = Path(override)
        return [
            Install(release="override", product=ENV_HOME, location=location, language="unknown")
        ]
    installs = _registry_installs()
    installs.sort(key=lambda i: _release_sort_key(i.release), reverse=True)
    return installs


def preferred_install() -> Install | None:
    installs = find_installs()
    # Prefer one that can actually do headless work over a merely-registered entry.
    for install in installs:
        if install.accoreconsole:
            return install
    return installs[0] if installs else None


def com_progids() -> list[str]:
    """Versioned `AutoCAD.Application.<n>` ProgIDs, most specific first.

    The bare `AutoCAD.Application` ProgID is deliberately excluded. On a real
    2026 install its `CurVer` can still point at an ancient release that is not
    present (observed: `.17`), so attaching through it fails with
    MK_E_UNAVAILABLE while the versioned ProgID works. Most sample code on the
    internet uses the bare one; this tool must not.
    """
    try:
        import winreg
    except ImportError:
        return []

    # Derive candidates from the installed releases and probe those directly.
    # Enumerating all of HKEY_CLASSES_ROOT to find them would mean walking tens
    # of thousands of keys on every `doctor` run.
    candidates: list[str] = []
    for install in find_installs():
        numbers = re.findall(r"\d+", install.release)
        if not numbers:
            continue
        major = numbers[0]
        if len(numbers) > 1:
            candidates.append(f"AutoCAD.Application.{major}.{numbers[1]}")
        candidates.append(f"AutoCAD.Application.{major}")

    found: list[str] = []
    for name in dict.fromkeys(candidates):
        try:
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, name).Close()
        except OSError:
            continue
        found.append(name)
    # More dotted components = more specific, then highest version.
    found.sort(key=lambda n: (n.count("."), _release_sort_key(n)), reverse=True)
    return found


def probe_com(timeout: float = 10.0) -> dict:
    """Attach to a *running* editor read-only and report what answered.

    Uses `GetActiveObject`, never `Dispatch`: dispatching would launch a second
    AutoCAD behind the operator's back when none is running.
    """
    progids = com_progids()
    if not progids:
        return {
            "available": False,
            "reason": "no versioned AutoCAD.Application ProgID is registered",
        }
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return {"available": False, "reason": "pywin32 is not installed", "progids": progids}

    pythoncom.CoInitialize()
    errors: dict[str, str] = {}
    for progid in progids:
        try:
            app = win32com.client.GetActiveObject(progid)
        except Exception as error:  # noqa: BLE001 - any COM failure is just "not this one"
            errors[progid] = type(error).__name__
            continue
        try:
            return {
                "available": True,
                "progid": progid,
                "version": str(app.Version),
                "documents": int(app.Documents.Count),
            }
        except Exception as error:  # noqa: BLE001
            return {
                "available": False,
                "progid": progid,
                "reason": f"attached but unreadable: {error}",
            }
    return {
        "available": False,
        "reason": "no running AutoCAD session answered",
        "progids": progids,
        "attempts": errors,
    }


def _decode(raw: bytes) -> str:
    """accoreconsole writes UTF-16LE; fall back rather than raise on oddities."""
    for encoding in ("utf-16-le", "utf-8", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_records(path: Path) -> list[tuple[str, str]]:
    """Read the side-channel file the script wrote.

    AutoLISP `write-line` uses the system codepage, so a Chinese layer name
    arrives as GBK bytes on this machine, not UTF-8.
    """
    raw = path.read_bytes()
    for encoding in ("utf-8", "mbcs", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    records, capturing = [], False
    for line in text.splitlines():
        line = line.strip()
        if line == _BEGIN:
            capturing = True
            continue
        if line == _END:
            break
        if capturing and "|" in line:
            key, _, value = line.partition("|")
            records.append((key, value))
    return records


def timeout_default() -> float:
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def build_script(body: str, out_path: Path) -> str:
    """Wrap a LISP body so its records land in `out_path`.

    `f` is the open output port the body writes with `(write-line ... f)`. The
    trailing bare `(princ)` suppresses the value echo described in the module
    docstring.
    """
    escaped = str(out_path).replace("\\", "\\\\")
    return (
        f'(setq f (open "{escaped}" "w"))\n'
        f'(write-line "{_BEGIN}" f)\n'
        f"{body.strip()}\n"
        f'(write-line "{_END}" f)\n'
        "(close f)\n"
        "(princ)\n"
    )


def run_script(
    body: str,
    *,
    drawing: Path | None = None,
    readonly: bool = True,
    timeout: float | None = None,
    install: Install | None = None,
) -> list[tuple[str, str]]:
    """Run a LISP body headlessly and return its `key|value` records.

    Never invokes the engine without `/s`; see the module docstring for why that
    would hang forever instead of failing.
    """
    install = install or preferred_install()
    if install is None:
        raise EngineError("E_CONFIG", "no AutoCAD installation was found", env_override=ENV_HOME)
    engine = install.accoreconsole
    if engine is None:
        raise EngineError(
            "E_CONFIG",
            "accoreconsole.exe is missing from the AutoCAD installation",
            location=str(install.location),
        )
    if drawing is not None and not drawing.is_file():
        raise EngineError("E_NOT_FOUND", "drawing file does not exist", drawing=str(drawing))

    timeout = timeout or timeout_default()

    with tempfile.TemporaryDirectory(prefix="auto-cad-cli-") as workspace:
        work = Path(workspace)
        out_path = work / "records.txt"
        script_path = work / "command.scr"
        script_path.write_text(build_script(body, out_path), encoding="utf-8")

        args = [str(engine)]
        if drawing is not None:
            args += ["/i", str(drawing)]
        args += ["/s", str(script_path)]
        if readonly:
            args.append("/readonly")

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                timeout=timeout,
                cwd=work,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            raise EngineError(
                "E_TIMEOUT",
                f"the AutoCAD core engine did not finish within {timeout:g}s",
                timeout_seconds=timeout,
                drawing=str(drawing) if drawing else None,
            ) from expired

        if not out_path.is_file():
            raise EngineError(
                "E_SERVER",
                "the AutoCAD core engine produced no result records",
                exit_code=completed.returncode,
                engine_output=_tail(_decode(completed.stdout)),
            )
        return _read_records(out_path)


def _tail(text: str, lines: int = 8) -> str:
    """Last few engine lines, for diagnosing a run that produced nothing."""
    meaningful = [line.strip() for line in text.splitlines() if line.strip()]
    return " / ".join(meaningful[-lines:])
