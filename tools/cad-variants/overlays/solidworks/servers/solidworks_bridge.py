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

VERSION = "0.1.0"
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


class ProtectedFiles:
    """Base files read at the start of a run. Writes while one is active are refused,
    and nothing is ever saved to a protected path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text("utf-8"))
            except ValueError:
                log(f"Ignoring unreadable state file {path}")

    def add(self, file_path: str, record: Dict[str, Any]) -> None:
        self.entries[normalise(file_path)] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2), "utf-8")

    def is_protected(self, file_path: str) -> bool:
        return normalise(file_path) in self.entries

    def check_writable(self, file_path: str) -> None:
        if self.is_protected(file_path):
            raise ToolError(
                f"Refused: {file_path} is a protected base model. Variants and estimating models are written "
                "to a copy, never the base. Use sw_save_copy_and_open first."
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

    def set_global(self, doc: Any, name: str, expression: str) -> str:
        manager = doc.GetEquationMgr()
        for index in range(manager.GetCount()):
            parsed = parse_equation(manager.Equation(index))
            if manager.GlobalVariable(index) and parsed and parsed["target"] == name:
                before = manager.Equation(index)
                self._put_indexed(manager, "Equation", index, f'"{name}" = {expression}')
                return before
        raise ToolError(f"No global variable named {name}. Nothing further was changed.")

    def rebuild(self, doc: Any) -> bool:
        return bool(doc.ForceRebuild3(False))

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
            "Protect the base file before any write; every write goes to a copy."
        ),
    )
    state: Dict[str, Optional[SolidWorksSession]] = {"session": None}

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
        }

    @server.tool(
        "sw_read_properties",
        "Rebuild health, mass, volume, density, material and bounding box of the active document. Mass is reported "
        "as unmeasured when a part has no material assigned. The SOLIDWORKS API Help calls the part bounding box "
        "approximate. Read-only." + UNVERIFIED,
    )
    def read_properties(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        errors = session().feature_errors(doc)
        failed = [e for e in errors if not e["warning"]]
        result: Dict[str, Any] = {
            "health": {"regenerated": not failed, "failed_features": failed, "warning_features": [e for e in errors if e["warning"]]},
        }
        if failed:
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
        "original is not saved and stays as it was on disk. Refuses an existing path and a protected path. Returns "
        "the active document's path, which must be the new one before any write." + UNVERIFIED,
        {"new_path": PATH},
        ["new_path"],
        read_only=False,
    )
    def save_copy_and_open(args: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        source = doc.GetPathName()
        if doc.GetSaveFlag():
            raise ToolError("The active document has unsaved changes. Ask the user to save or discard them first.")
        target = check_new_path(args["new_path"], protected)
        if source and Path(source).name.lower() == target.name.lower():
            raise ToolError(
                "Refused: the copy has the same file name as the open original. SOLIDWORKS cannot open two documents "
                "with one name at once, so the copy would not become active. Choose another file name."
            )
        result = session().save_copy(doc, str(target))
        if not result["ok"] or not target.exists():
            raise ToolError(f"SOLIDWORKS did not write the copy (errors {result['errors']}, warnings {result['warnings']}).")
        active = session().open_and_activate(str(target), doc.GetType())
        active_path = active.GetPathName()
        if normalise(active_path) != normalise(str(target)):
            raise ToolError(f"The copy was written, but the active document is {active_path}, not the copy. Stopping before any write.")
        return {"source": source, "copy": active_path, "active_is_copy": True}

    @server.tool(
        "sw_apply_parameters",
        "Change dimensions (full name -> value in SI units) and global variables (name -> expression with units) in "
        "the active document, then rebuild. Refuses unless the active document is expected_path, and refuses a "
        "protected base. Returns each change from -> to and the rebuild health. Does not save." + UNVERIFIED,
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
        changes = []
        for name, expression in (args.get("global_variables") or {}).items():
            before = session().set_global(doc, name, str(expression))
            changes.append({"name": name, "kind": "global_variable", "from": before, "to": f'"{name}" = {expression}'})
        current = {d["name"]: d for d in session().dimensions(doc)} if args.get("dimensions") else {}
        for name, value in (args.get("dimensions") or {}).items():
            if name in current and not current[name]["driving"]:
                raise ToolError(f"{name} is not a driving dimension. Stopped; earlier changes in this call: {changes}")
            status = session().set_dimension(doc, name, value)
            changes.append({"name": name, "kind": "dimension", "from": current.get(name, {}).get("value_si"), "to": value, "status": status})
            if status != "Successful":
                raise ToolError(f"SOLIDWORKS refused {name} = {value}: {status}. Changes so far: {changes}")
        session().rebuild(doc)
        errors = session().feature_errors(doc)
        failed = [e for e in errors if not e["warning"]]
        return {"changes": changes, "health": {"regenerated": not failed, "failed_features": failed}, "saved": False}

    @server.tool(
        "sw_save_active",
        "Save the active document in place. Refuses unless it is expected_path, and refuses a protected base." + UNVERIFIED,
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
