#!/usr/bin/env python3
"""SOLIDWORKS bridge: the CAD adapter surface of manufacturing-solidworks, over MCP stdio.

Implements the variant operations `skills/design-space-explore/reference/cad-adapter.md`
lists, against the SOLIDWORKS API through COM. It attaches to the SOLIDWORKS
session already running on this Windows machine and acts on the document open
in it; it never starts SOLIDWORKS and never opens the base model itself.

Needs: Windows, SOLIDWORKS running, Python 3.10+, and pywin32 (`pip install pywin32`).

    SOLIDWORKS_BRIDGE_STATE   file that remembers protected base files
                              default %USERPROFILE%\\.manufacturing-solidworks\\protected.json

Status: written against the SOLIDWORKS API Help (method names, signatures and
enum values checked). NOT yet run against a live SOLIDWORKS session. Every tool
says so in its description until it has been.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_stdio import Server, ToolError, log  # noqa: E402

VERSION = "0.2.0"
UNVERIFIED = " (Not yet run against a live SOLIDWORKS session.)"

# Enum values, from the SOLIDWORKS API Help's swconst pages.
SW_DOC_PART, SW_DOC_ASSEMBLY, SW_DOC_DRAWING = 1, 2, 3                 # swDocumentTypes_e
SW_SAVE_AS_CURRENT_VERSION = 0                                           # swSaveAsVersion_e
SW_SAVE_AS_SILENT, SW_SAVE_AS_COPY = 1, 2                                # swSaveAsOptions_e
SW_OPEN_SILENT = 1                                                       # swOpenDocOptions_e
SW_REBUILD_ACTIVE_DOC = 2                                                # swRebuildOnActivation_e
SW_SET_VALUE_IN_THIS_CONFIGURATION = 1                                   # swSetValueInConfiguration_e
SW_DIMENSION_DRIVEN, SW_DIMENSION_DRIVING = 1, 2                         # swDimensionDrivenState_e
SET_VALUE_STATUS = {0: "Successful", 1: "Failure", 2: "InvalidValue", 3: "DrivenDimension", 4: "ModelNotLoaded", 5: "FrozenFeatureOwner"}
DOC_TYPES = {SW_DOC_PART: "part", SW_DOC_ASSEMBLY: "assembly", SW_DOC_DRAWING: "drawing"}

QUOTED = re.compile(r'"([^"]+)"')


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without SOLIDWORKS)
# ---------------------------------------------------------------------------


def file_fingerprint(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": stat.st_size, "mtime": stat.st_mtime}


def normalise(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def parse_equation(text: str) -> Optional[Dict[str, Any]]:
    """Split one SOLIDWORKS equation line into its left side and the names it references.

    Lines read like `"wall" = 3mm` (a global variable) or
    `"D1@Boss-Extrude1" = "wall" * 2` (a dimension equation).
    """
    if "=" not in text:
        return None
    left, right = text.split("=", 1)
    left_names = QUOTED.findall(left)
    if not left_names:
        return None
    return {"target": left_names[0], "references": QUOTED.findall(right), "expression": right.strip()}


def global_variable_drivers(equations: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """For each global variable, the dimensions its value reaches, directly or through other globals.

    `equations` items: {"text": str, "global_variable": bool}. A dimension
    target is any name containing "@".
    """
    parsed = [(e, parse_equation(e.get("text", ""))) for e in equations]
    globals_ = [p["target"] for e, p in parsed if p and e.get("global_variable")]
    feeds: Dict[str, List[str]] = {}
    for _, p in parsed:
        if not p:
            continue
        for reference in p["references"]:
            feeds.setdefault(reference, []).append(p["target"])

    def reach(name: str, trail: frozenset) -> List[str]:
        found: List[str] = []
        for target in feeds.get(name, []):
            if target in trail:
                continue
            if "@" in target:
                found.append(target)
            else:
                found.extend(reach(target, trail | {name}))
        return sorted(set(found))

    return {
        name: {"drives_dimensions": reach(name, frozenset()), "drives_geometry": bool(reach(name, frozenset()))}
        for name in globals_
    }


STATE_FORMAT = 2
INTEGRITY_NOTE = (
    "The feature list is read from the FeatureManager tree (sketches are the features of type ProfileFeature). "
    "The bridge cannot list bodies or external references (in-context or derived references) yet: record the "
    "integrity check as not_checked, with the feature lists, and external references as unknown. Never record "
    "them as clean or absent."
)


class StateLock:
    """A lock file beside the state file, so two bridges never write the state at once.

    Created with O_EXCL; a lock older than `stale_after` seconds is taken to be left
    by a bridge that died, and removed.
    """

    def __init__(self, path: Path, timeout: float = 10.0, stale_after: float = 120.0) -> None:
        self.path = path.with_name(path.name + ".lock")
        self.timeout = timeout
        self.stale_after = stale_after

    def __enter__(self) -> "StateLock":
        import time

        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, str(os.getpid()).encode())
                os.close(handle)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise ToolError(
                        f"Another bridge is holding {self.path}. Wait for it to finish, then ask again. Nothing was changed."
                    )
                time.sleep(0.05)

    def __exit__(self, *_: Any) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def write_json_atomically(path: Path, data: Any) -> None:
    """Under the state lock, write to a temporary file in the same folder, flush it to disk, then
    rename it over the target, so a crash leaves either the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with StateLock(path):
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


class ProtectedFiles:
    """What the bridge may and may not write, persisted so a restarted bridge keeps refusing.

    `protected`: base files read at the start of a run. Never written.
    `copies`: files this bridge made with sw_save_copy_and_open. The only files it writes.

    If the state file exists but cannot be read, nothing is written at all until a
    person fixes or removes it (fail closed): an empty list would silently drop
    the protection of every base recorded in it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.copies: Dict[str, Dict[str, Any]] = {}
        self.error: Optional[str] = None
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            if data.get("format") == STATE_FORMAT:
                protected, copies = data.get("protected"), data.get("copies")
                if not isinstance(protected, dict) or not isinstance(copies, dict):
                    raise ValueError("protected and copies must be objects")
            elif "format" not in data and all(isinstance(v, dict) and "sha256" in v for v in data.values()):
                protected, copies = data, {}  # the first version's file: protected bases only
            else:
                raise ValueError("unrecognised layout")
            for record in list(protected.values()) + list(copies.values()):
                if not isinstance(record, dict):
                    raise ValueError("an entry is not an object")
            self.entries, self.copies = protected, copies
        except (OSError, ValueError) as err:
            self.error = f"The bridge's state file {path} cannot be read ({err})."
            log(self.error + " Refusing every write until it is fixed or removed.")

    def _require_readable(self) -> None:
        if self.error:
            raise ToolError(
                self.error + " It records which base files are protected, so the bridge writes nothing until "
                "a person checks it, then fixes or removes it. Nothing was changed."
            )

    def _save(self) -> None:
        write_json_atomically(self.path, {"format": STATE_FORMAT, "protected": self.entries, "copies": self.copies})

    def add(self, file_path: str, record: Dict[str, Any]) -> None:
        self._require_readable()
        key = normalise(file_path)
        if key in self.copies:
            raise ToolError(f"{file_path} is a copy this bridge made, not a base model.")
        self.entries[key] = record
        self._save()

    def register_copy(self, file_path: str, record: Dict[str, Any]) -> None:
        self._require_readable()
        key = normalise(file_path)
        if key in self.entries:
            raise ToolError(f"Refused: {file_path} is a protected base path.")
        self.copies[key] = record
        self._save()

    def retire_copy(self, file_path: str, reason: str) -> None:
        """Stop writing to a copy whose state is no longer known."""
        record = self.copies.get(normalise(file_path))
        if record is not None:
            record["retired"] = reason
            try:
                self._save()
            except OSError as err:  # the in-memory mark still refuses writes for this session
                log(f"Could not record the retired copy: {err}")

    def is_protected(self, file_path: str) -> bool:
        return normalise(file_path) in self.entries

    def is_copy(self, file_path: str) -> bool:
        return normalise(file_path) in self.copies

    def check_writable(self, file_path: str) -> None:
        self._require_readable()
        if self.is_protected(file_path):
            raise ToolError(
                f"Refused: {file_path} is a protected base model. Variants and estimating models are written "
                "to a copy, never the base. Use sw_save_copy_and_open first."
            )
        record = self.copies.get(normalise(file_path))
        if record is None:
            raise ToolError(
                f"Refused: {file_path} is not a copy this bridge made. The bridge writes only to a copy made "
                "with sw_save_copy_and_open, never to a file it did not create. Nothing was changed."
            )
        if record.get("retired"):
            raise ToolError(
                f"Refused: the copy {file_path} was left in an unknown state ({record['retired']}). Discard it and "
                "make a new copy. Nothing was changed."
            )


def check_new_path(new_path: str, protected: ProtectedFiles) -> Path:
    target = Path(new_path)
    if not target.is_absolute():
        raise ToolError("new_path must be an absolute path, e.g. C:\\Estimates\\EH-RFQ-0912_L1_PN-4471-B_R2.SLDPRT")
    if target.suffix.upper() not in (".SLDPRT", ".SLDASM"):
        raise ToolError("new_path must end in .SLDPRT or .SLDASM.")
    if target.exists():
        raise ToolError(f"Refused: {new_path} already exists. This connector never overwrites a file.")
    if protected.is_protected(new_path):
        raise ToolError(f"Refused: {new_path} is a protected base path.")
    if not target.parent.exists():
        raise ToolError(f"The folder {target.parent} does not exist. Create it or choose another.")
    return target


# ---------------------------------------------------------------------------
# COM session
# ---------------------------------------------------------------------------


class SolidWorksSession:
    """Thin wrapper over the SOLIDWORKS COM objects. Every COM call lives here."""

    def __init__(self) -> None:
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError as err:
            raise ToolError(
                "pywin32 is not installed for the Python that runs this connector. Run `pip install pywin32` on "
                "the Windows machine running SOLIDWORKS, then restart Claude."
            ) from err
        self.pythoncom = pythoncom
        self.client = win32com.client
        try:
            self.app = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception as err:  # noqa: BLE001
            raise ToolError(
                "SOLIDWORKS is not running on this machine, or this connector cannot reach it. Open SOLIDWORKS "
                "with the model, then ask again. This connector attaches to a running session; it never starts one."
            ) from err

    # out-parameter helpers ---------------------------------------------------

    def _out_int(self) -> Any:
        return self.client.VARIANT(self.pythoncom.VT_BYREF | self.pythoncom.VT_I4, 0)

    def _out_bool(self) -> Any:
        return self.client.VARIANT(self.pythoncom.VT_BYREF | self.pythoncom.VT_BOOL, False)

    def _out_str(self) -> Any:
        return self.client.VARIANT(self.pythoncom.VT_BYREF | self.pythoncom.VT_BSTR, "")

    def _put_indexed(self, obj: Any, name: str, index: int, value: Any) -> None:
        dispid = obj._oleobj_.GetIDsOfNames(name)
        obj._oleobj_.Invoke(dispid, 0, self.pythoncom.DISPATCH_PROPERTYPUT, False, index, value)

    # reads -------------------------------------------------------------------

    def revision(self) -> str:
        return str(self.app.RevisionNumber())

    def active_doc(self) -> Any:
        doc = self.app.ActiveDoc
        if doc is None:
            raise ToolError("No document is open and active in SOLIDWORKS. Open the model, then ask again.")
        return doc

    def describe(self, doc: Any) -> Dict[str, Any]:
        doc_type = doc.GetType()
        info = {
            "title": doc.GetTitle(),
            "path": doc.GetPathName() or None,
            "type": DOC_TYPES.get(doc_type, str(doc_type)),
            "unsaved_changes": bool(doc.GetSaveFlag()),
            "active_configuration": doc.GetActiveConfiguration().Name,
            "configurations": list(doc.GetConfigurationNames() or []),
        }
        if doc_type == SW_DOC_PART:
            database = self._out_str()
            info["material"] = doc.GetMaterialPropertyName2("", database) or None
        return info

    def _walk_features(self, doc: Any) -> Iterable[Any]:
        feature = doc.FirstFeature()
        while feature is not None:
            yield feature
            sub = feature.GetFirstSubFeature()
            while sub is not None:
                yield sub
                sub = sub.GetNextSubFeature()
            feature = feature.GetNextFeature()

    def dimensions(self, doc: Any) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for feature in self._walk_features(doc):
            display = feature.GetFirstDisplayDimension()
            while display is not None:
                dimension = display.GetDimension2(0)
                full_name = dimension.FullName
                if full_name not in seen:
                    seen.add(full_name)
                    state = dimension.DrivenState
                    out.append(
                        {
                            "name": full_name,
                            "value_si": dimension.SystemValue,
                            "driving": state == SW_DIMENSION_DRIVING,
                            "driven_state": {SW_DIMENSION_DRIVEN: "driven", SW_DIMENSION_DRIVING: "driving"}.get(state, "unknown"),
                            "read_only": bool(dimension.ReadOnly),
                            "feature": feature.Name,
                        }
                    )
                display = feature.GetNextDisplayDimension(display)
        return out

    def equations(self, doc: Any) -> List[Dict[str, Any]]:
        manager = doc.GetEquationMgr()
        out = []
        for index in range(manager.GetCount()):
            out.append(
                {
                    "index": index,
                    "text": manager.Equation(index),
                    "value": manager.Value(index),
                    "global_variable": bool(manager.GlobalVariable(index)),
                }
            )
        return out

    def feature_list(self, doc: Any) -> List[Dict[str, Any]]:
        return [{"name": f.Name, "type": f.GetTypeName2()} for f in self._walk_features(doc)]

    def feature_errors(self, doc: Any) -> List[Dict[str, Any]]:
        problems = []
        for feature in self._walk_features(doc):
            is_warning = self._out_bool()
            code = feature.GetErrorCode2(is_warning)
            if code:
                problems.append(
                    {"feature": feature.Name, "type": feature.GetTypeName2(), "error_code": code, "warning": bool(is_warning.value)}
                )
        return problems

    def properties(self, doc: Any) -> Dict[str, Any]:
        mass = doc.Extension.CreateMassProperty2()
        result: Dict[str, Any] = {
            "mass_kg": mass.Mass if mass is not None else None,
            "volume_m3": mass.Volume if mass is not None else None,
            "density_kg_m3": mass.Density if mass is not None else None,
        }
        if doc.GetType() == SW_DOC_PART:
            box = doc.GetPartBox(True)
            result["bounding_box_m"] = list(box) if box is not None else None
        return result

    # writes ------------------------------------------------------------------

    def save_copy(self, doc: Any, new_path: str) -> Dict[str, Any]:
        errors, warnings = self._out_int(), self._out_int()
        ok = doc.Extension.SaveAs3(
            new_path, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_SILENT | SW_SAVE_AS_COPY, None, None, errors, warnings
        )
        return {"ok": bool(ok), "errors": errors.value, "warnings": warnings.value}

    def open_and_activate(self, path: str, doc_type: int) -> Any:
        errors, warnings = self._out_int(), self._out_int()
        doc = self.app.OpenDoc6(path, doc_type, SW_OPEN_SILENT, "", errors, warnings)
        if doc is None:
            raise ToolError(f"SOLIDWORKS could not open {path} (error {errors.value}, warning {warnings.value}).")
        activate_errors = self._out_int()
        self.app.ActivateDoc3(doc.GetTitle(), False, SW_REBUILD_ACTIVE_DOC, activate_errors)
        return self.active_doc()

    def set_dimension(self, doc: Any, full_name: str, value_si: float) -> str:
        dimension = doc.Parameter(full_name)
        if dimension is None:
            raise ToolError(f"No dimension named {full_name} in the active document. Nothing further was changed.")
        status = dimension.SetSystemValue3(float(value_si), SW_SET_VALUE_IN_THIS_CONFIGURATION, None)
        return SET_VALUE_STATUS.get(status, str(status))

    def find_global(self, doc: Any, name: str) -> Optional[Dict[str, Any]]:
        manager = doc.GetEquationMgr()
        for index in range(manager.GetCount()):
            parsed = parse_equation(manager.Equation(index))
            if manager.GlobalVariable(index) and parsed and parsed["target"] == name:
                return {"index": index, "text": manager.Equation(index)}
        return None

    def put_equation(self, doc: Any, index: int, text: str) -> None:
        self._put_indexed(doc.GetEquationMgr(), "Equation", index, text)

    def rebuild(self, doc: Any) -> bool:
        """True only when SOLIDWORKS reports the rebuild succeeded. An exception counts as a failure."""
        try:
            return bool(doc.ForceRebuild3(False))
        except Exception as err:  # noqa: BLE001
            log(f"ForceRebuild3 raised: {err}")
            return False

    def save(self, doc: Any) -> Dict[str, Any]:
        errors, warnings = self._out_int(), self._out_int()
        ok = doc.Save3(SW_SAVE_AS_SILENT, errors, warnings)
        return {"ok": bool(ok), "errors": errors.value, "warnings": warnings.value}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

PATH = {"type": "string", "minLength": 1}


def build_server(session_factory: Callable[[], SolidWorksSession], protected: ProtectedFiles) -> Server:
    server = Server(
        name="solidworks-bridge",
        version=VERSION,
        instructions=(
            "CAD adapter for manufacturing-solidworks. Read cad-adapter.md before the first call. "
            "Every write goes to a copy made with sw_save_copy_and_open; the bridge refuses any other file."
        ),
    )
    state: Dict[str, Optional[SolidWorksSession]] = {"session": None}
    stale: Dict[str, str] = {}  # copies whose last rebuild failed: geometry on screen may be stale

    def health_of(doc: Any, rebuild_ok: Optional[bool]) -> Dict[str, Any]:
        """Regeneration health. A rebuild SOLIDWORKS reported as failed is a failure even when no
        feature shows an error: the geometry may be the previous variant's."""
        path = normalise(doc.GetPathName() or doc.GetTitle())
        if rebuild_ok is False:
            stale[path] = "the last rebuild reported failure"
        elif rebuild_ok is True:
            stale.pop(path, None)
        errors = session().feature_errors(doc)
        failed = [e for e in errors if not e["warning"]]
        health: Dict[str, Any] = {
            "regenerated": not failed and path not in stale,
            "rebuild_reported_success": rebuild_ok if rebuild_ok is not None else (path not in stale),
            "failed_features": failed,
            "warning_features": [e for e in errors if e["warning"]],
        }
        if path in stale:
            health["note"] = (
                "SOLIDWORKS reported the rebuild as failed, so the geometry may be stale. This variant failed: "
                "do not measure it or record its properties."
            )
        return health

    def session() -> SolidWorksSession:
        if state["session"] is None:
            state["session"] = session_factory()
        return state["session"]

    def active_for_write(expected_path: str) -> Any:
        doc = session().active_doc()
        path = doc.GetPathName() or ""
        if not path or normalise(path) != normalise(expected_path):
            raise ToolError(
                f"Refused: the active document is {path or 'an unsaved document'}, not {expected_path}. Nothing was changed."
            )
        protected.check_writable(path)
        return doc

    @server.tool("sw_status", "Check the connection: SOLIDWORKS revision and the active document, if any. Read-only." + UNVERIFIED)
    def status(_: Dict[str, Any]) -> Dict[str, Any]:
        s = session()
        doc = s.app.ActiveDoc
        return {
            "solidworks_revision": s.revision(),
            "active_document": s.describe(doc) if doc is not None else None,
            "protected_files": len(protected.entries),
            "bridge_copies": len(protected.copies),
            "state_error": protected.error,
        }

    @server.tool(
        "sw_read_active_document",
        "Name, path, type, configurations, material and unsaved-changes flag of the active document. Read-only." + UNVERIFIED,
    )
    def read_active(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        info = session().describe(doc)
        info["protected"] = bool(info.get("path")) and protected.is_protected(info["path"])
        return info

    @server.tool(
        "sw_protect_base",
        "Mark the active document's file as a base model: record its SHA-256, size and modified time, and refuse "
        "every write to it from now on. Refuses a document with unsaved changes. Call before the first write of "
        "any run that starts from an existing model." + UNVERIFIED,
    )
    def protect_base(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        path = doc.GetPathName()
        if not path:
            raise ToolError("The active document has never been saved, so there is no base file to protect.")
        if doc.GetSaveFlag():
            raise ToolError(
                "The active document has unsaved changes. Someone has work open in it, and a copy would carry that "
                "work. Ask the user to save or discard it themselves."
            )
        record = {"title": doc.GetTitle(), **file_fingerprint(Path(path)), "protected_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        protected.add(path, record)
        return {"path": path, **record}

    @server.tool(
        "sw_check_base_untouched",
        "Confirm a protected base file's bytes are unchanged since it was protected (SHA-256). A changed file means "
        "the base was saved over: stop and tell the user. Read-only." + UNVERIFIED,
        {"path": PATH},
        ["path"],
    )
    def check_untouched(args: Dict[str, Any]) -> Dict[str, Any]:
        key = normalise(args["path"])
        record = protected.entries.get(key)
        if not record:
            raise ToolError(f"{args['path']} was never protected, so there is no recorded fingerprint to compare.")
        if not record.get("sha256"):
            raise ToolError(f"The record for {args['path']} has no fingerprint, so the bridge cannot say whether it changed.")
        if not Path(args["path"]).exists():
            return {"path": args["path"], "untouched": False, "note": "The base file is no longer at this path. Stop and tell the user."}
        now = file_fingerprint(Path(args["path"]))
        return {
            "path": args["path"],
            "untouched": now["sha256"] == record["sha256"],
            "recorded": {k: record[k] for k in ("sha256", "size", "mtime")},
            "current": now,
        }

    @server.tool(
        "sw_read_parameters",
        "Read what can be varied in the active document: every dimension with its full name, value in SI units "
        "(metres, radians) and driving/driven state, every equation, and for each global variable the dimensions "
        "its value reaches through equations. A driven dimension or a global variable that reaches no dimension "
        "does not drive geometry. Read-only." + UNVERIFIED,
    )
    def read_parameters(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        equations = session().equations(doc)
        return {
            "document": doc.GetPathName() or doc.GetTitle(),
            "dimensions": session().dimensions(doc),
            "equations": equations,
            "global_variables": global_variable_drivers(equations),
            "units": "SI: metres for lengths, radians for angles",
            "features": session().feature_list(doc),
            "integrity_note": INTEGRITY_NOTE,
        }

    @server.tool(
        "sw_read_properties",
        "Rebuild health, mass, volume, density, material and bounding box of the active document. Mass is reported "
        "as unmeasured when a part has no material assigned. The SOLIDWORKS API Help calls the part bounding box "
        "approximate. Read-only." + UNVERIFIED,
    )
    def read_properties(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        health = health_of(doc, None)
        result: Dict[str, Any] = {"health": health}
        if not health["regenerated"]:
            result["properties"] = None
            result["note"] = "Failed rebuild: not measured."
            return result
        properties = session().properties(doc)
        material = session().describe(doc).get("material") if doc.GetType() == SW_DOC_PART else None
        if doc.GetType() == SW_DOC_PART and not material:
            properties["mass_kg"] = None
            properties["mass_note"] = "No material assigned: SOLIDWORKS computes mass from a default density. Mass is unmeasured, not this number."
        properties["material"] = material
        properties["bounding_box_note"] = "Approximate per the SOLIDWORKS API Help (IPartDoc::GetPartBox). Do not use it as a read-back check."
        result["properties"] = properties
        return result

    @server.tool(
        "sw_save_copy_and_open",
        "Save the active document as a copy at a NEW path, open the copy and make it the active document. The "
        "original is not saved and stays as it was on disk. If the original is not yet protected, it is protected "
        "first (its SHA-256 recorded). Refuses an existing path, a protected path, and copying a copy. Registers the "
        "copy as the only kind of file the bridge writes. Returns the active document's path, which must be the new "
        "one before any write." + UNVERIFIED,
        {"new_path": PATH},
        ["new_path"],
        read_only=False,
    )
    def save_copy_and_open(args: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        source = doc.GetPathName()
        if doc.GetSaveFlag():
            raise ToolError("The active document has unsaved changes. Ask the user to save or discard them first.")
        if not source:
            raise ToolError("The active document has never been saved, so it has no file to copy from. Ask the user to save it first.")
        protected._require_readable()
        target = check_new_path(args["new_path"], protected)
        if protected.is_copy(source):
            raise ToolError(
                "Refused: the active document is already a copy this bridge made. Make each copy from the base "
                "model, so every variant starts from the same geometry."
            )
        if source and Path(source).name.lower() == target.name.lower():
            raise ToolError(
                "Refused: the copy has the same file name as the open original. SOLIDWORKS cannot open two documents "
                "with one name at once, so the copy would not become active. Choose another file name."
            )
        protected_now = False
        if not protected.is_protected(source):
            # The base is protected here, by the bridge, rather than trusting that sw_protect_base ran first.
            record = {"title": doc.GetTitle(), **file_fingerprint(Path(source)), "protected_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
            protected.add(source, record)
            protected_now = True
        result = session().save_copy(doc, str(target))
        if not result["ok"] or not target.exists():
            raise ToolError(f"SOLIDWORKS did not write the copy (errors {result['errors']}, warnings {result['warnings']}).")
        try:
            protected.register_copy(str(target), {"source": source, "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        except OSError as err:
            raise ToolError(f"The copy was written to {target}, but the bridge could not record it ({err}), so it will not write to it.") from err
        active = session().open_and_activate(str(target), doc.GetType())
        active_path = active.GetPathName()
        if normalise(active_path) != normalise(str(target)):
            raise ToolError(f"The copy was written, but the active document is {active_path}, not the copy. Stopping before any write.")
        return {"source": source, "source_protected_by_this_call": protected_now, "copy": active_path, "active_is_copy": True}

    @server.tool(
        "sw_apply_parameters",
        "Change dimensions (full name -> value in SI units) and global variables (name -> expression with units) in "
        "the active document, then rebuild. Writes only to a copy this bridge made, and only when it is the active "
        "document at expected_path. Checks the whole set before changing anything, and undoes the set if a change "
        "fails part way. Returns each change from -> to and the rebuild health; a rebuild SOLIDWORKS reports as "
        "failed is a failed variant. Does not save." + UNVERIFIED,
        {
            "expected_path": PATH,
            "dimensions": {"type": "object", "description": "e.g. {\"D1@Boss-Extrude1\": 0.0035}"},
            "global_variables": {"type": "object", "description": "e.g. {\"wall\": \"3.5mm\"}"},
        },
        ["expected_path"],
        read_only=False,
    )
    def apply_parameters(args: Dict[str, Any]) -> Dict[str, Any]:
        doc = active_for_write(args["expected_path"])
        dimension_values = args.get("dimensions") or {}
        global_values = args.get("global_variables") or {}
        if not isinstance(dimension_values, dict) or not isinstance(global_values, dict):
            raise ToolError("dimensions and global_variables must be objects. Nothing was changed.")
        if not dimension_values and not global_values:
            raise ToolError("No dimensions or global variables to change. Nothing was changed.")

        # 1. Validate the whole batch before changing anything.
        problems: List[str] = []
        current = {d["name"]: d for d in session().dimensions(doc)} if dimension_values else {}
        equation_targets = set()
        if dimension_values:
            for equation in session().equations(doc):
                parsed = parse_equation(equation.get("text", ""))
                if parsed and not equation.get("global_variable"):
                    equation_targets.add(parsed["target"])
        for name, value in dimension_values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
                problems.append(f"{name}: {value!r} is not a number in SI units")
            elif name not in current:
                problems.append(f"{name}: no such dimension in the active document")
            elif not current[name]["driving"]:
                problems.append(f"{name}: not a driving dimension")
            elif current[name]["read_only"]:
                problems.append(f"{name}: read-only")
            elif name in equation_targets:
                problems.append(f"{name}: set by an equation, which would overwrite it at the next rebuild; vary the global variable it reads")
        found_globals: Dict[str, Dict[str, Any]] = {}
        for name, expression in global_values.items():
            if not isinstance(expression, (str, int, float)) or isinstance(expression, bool) or not str(expression).strip():
                problems.append(f"{name}: {expression!r} is not an expression")
                continue
            found = session().find_global(doc, name)
            if found is None:
                problems.append(f"{name}: no such global variable")
            else:
                found_globals[name] = found
        if problems:
            raise ToolError("Nothing was changed. This parameter set cannot be applied: " + "; ".join(problems) + ".")

        # 2. Apply, remembering how to undo each change.
        changes: List[Dict[str, Any]] = []
        undo: List[Callable[[], None]] = []
        try:
            for name, expression in global_values.items():
                found = found_globals[name]
                new_text = f'"{name}" = {expression}'
                session().put_equation(doc, found["index"], new_text)
                undo.append(lambda i=found["index"], text=found["text"]: session().put_equation(doc, i, text))
                changes.append({"name": name, "kind": "global_variable", "from": found["text"], "to": new_text})
            for name, value in dimension_values.items():
                before = current[name]["value_si"]
                status = session().set_dimension(doc, name, value)
                if status == "Successful":
                    undo.append(lambda n=name, v=before: session().set_dimension(doc, n, v))
                    changes.append({"name": name, "kind": "dimension", "from": before, "to": value, "status": status})
                else:
                    raise ToolError(f"SOLIDWORKS refused {name} = {value}: {status}")
        except Exception as err:  # noqa: BLE001
            not_undone = []
            for step in reversed(undo):
                try:
                    step()
                except Exception as undo_err:  # noqa: BLE001
                    not_undone.append(str(undo_err))
            rebuilt = session().rebuild(doc) if undo else True
            reason = str(err)
            if not_undone or not rebuilt:
                protected.retire_copy(args["expected_path"], "a parameter change failed and could not be fully undone")
                raise ToolError(
                    f"{reason}. Undoing the earlier changes in this call did not fully succeed, so this copy is in an "
                    "unknown state: the bridge will not write to it again. Discard it and make a new copy. Nothing was saved."
                ) from err
            raise ToolError(
                f"{reason}. The {len(undo)} earlier change(s) in this call were undone, so the copy is as it was before "
                "this call. Nothing was saved."
            ) from err

        # 3. Rebuild, and believe SOLIDWORKS when it says the rebuild failed.
        rebuild_ok = session().rebuild(doc)
        return {"changes": changes, "health": health_of(doc, rebuild_ok), "saved": False}

    @server.tool(
        "sw_save_active",
        "Save the active document in place. Saves only a copy this bridge made, and only when it is the active "
        "document at expected_path." + UNVERIFIED,
        {"expected_path": PATH},
        ["expected_path"],
        read_only=False,
    )
    def save_active(args: Dict[str, Any]) -> Dict[str, Any]:
        doc = active_for_write(args["expected_path"])
        result = session().save(doc)
        if not result["ok"]:
            raise ToolError(f"SOLIDWORKS did not save (errors {result['errors']}, warnings {result['warnings']}).")
        return {"saved": args["expected_path"], **result}

    return server


def main() -> None:
    default_state = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".manufacturing-solidworks" / "protected.json"
    state_path = Path(os.environ.get("SOLIDWORKS_BRIDGE_STATE", str(default_state)))
    build_server(SolidWorksSession, ProtectedFiles(state_path)).serve()


if __name__ == "__main__":
    main()
