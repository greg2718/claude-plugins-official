#!/usr/bin/env python3
"""SOLIDWORKS bridge: the CAD adapter surface of manufacturing-solidworks, over MCP stdio.

Implements the operations `skills/design-space-explore/reference/cad-adapter.md`
lists (variants, open-apply-save-as-new, building a part from a drawing, STEP
import, and the integrity check), against the SOLIDWORKS API through COM. It
attaches to the SOLIDWORKS session already running on this Windows machine; it
never starts SOLIDWORKS and never opens the base model itself. It writes only to
files it made: copies, new parts and imported parts. CAM is not implemented.

Needs: Windows, SOLIDWORKS running, Python 3.10+, and pywin32 (`pip install pywin32`).

    SOLIDWORKS_BRIDGE_STATE   file that remembers protected base files
                              default %USERPROFILE%\\.manufacturing-solidworks\\protected.json
    SOLIDWORKS_PART_TEMPLATE  optional .prtdot for sw_new_part (default: SOLIDWORKS's default part template)

Status: written against the SOLIDWORKS API Help (method names, signatures and
enum values checked; the feature-building calls against a markdown mirror of it). NOT yet run against a live SOLIDWORKS session. Every tool
says so in its description until it has been.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_stdio import Server, ToolError, log  # noqa: E402

VERSION = "0.4.0"
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

# Feature-building enum values, from a markdown mirror of the SOLIDWORKS API Help's swconst pages.
SW_END_BLIND, SW_END_THROUGH_ALL = 0, 1                                  # swEndConditions_e
SW_START_SKETCH_PLANE = 0                                                # swStartConditions_e
SW_SOLID_BODY = 0                                                        # swBodyType_e
SW_FILLET_UNIFORM_RADIUS = 2                                             # swFeatureFilletOptions_e
SW_FILLET_TYPE_SIMPLE = 0                                                # swFeatureFilletType_e
SW_FILLET_OVERFLOW_DEFAULT = 0                                           # swFilletOverFlowType_e
SW_CHAMFER_EQUAL_DISTANCE = 16                                           # swChamferType_e
# swUserPreferenceStringValue_e.swDefaultTemplatePart: the mirror lists the member without its number.
# 8 is the value SOLIDWORKS macros use; the template path read with it is checked before use, and
# SOLIDWORKS_PART_TEMPLATE overrides it.
SW_DEFAULT_TEMPLATE_PART = 8
EDGE_TOLERANCE_M = 1e-6
MAX_IMPORT_BYTES = 200 * 1024 * 1024
PLANES = ("Front", "Top", "Right")
EXTRUDE_OPERATIONS = ("NEW", "ADD", "REMOVE")
END_BOUNDS = ("BLIND", "THROUGH_ALL")


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
    "Use sw_list_contents before and after a step for the integrity check: it lists the FeatureManager tree "
    "(sketches are the features of type ProfileFeature), solid bodies, and the count of external references "
    "SOLIDWORKS reports. Record the check as clean only when both reads succeeded and the after-list holds "
    "exactly the before-list plus what the step made."
)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise ToolError(f"{label} must be a finite number in millimetres, not {value!r}. Nothing was changed.")
    return float(value)


def _positive(value: Any, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ToolError(f"{label} must be greater than zero, not {value!r}. Nothing was changed.")
    return number


def feature_spec(kind: str, spec: Any) -> Dict[str, Any]:
    """Check a build step completely and convert it to SI units (metres) before anything is changed."""
    if not isinstance(spec, dict):
        raise ToolError("spec must be an object. Nothing was changed.")
    mm = 0.001
    if kind == "sketch":
        plane = spec.get("plane")
        if plane not in PLANES:
            raise ToolError(f"plane must be one of {', '.join(PLANES)}. Nothing was changed.")
        entities = spec.get("entities")
        if not isinstance(entities, list) or not entities:
            raise ToolError("A sketch needs at least one entity (rectangle, circle or line). Nothing was changed.")
        out = []
        for index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                raise ToolError(f"Sketch entity {index} is not an object. Nothing was changed.")
            kind_of = entity.get("type")
            if kind_of in ("rectangle", "line"):
                x1, y1, x2, y2 = (_finite(entity.get(k), f"entity {index} {k}") * mm for k in ("x1", "y1", "x2", "y2"))
                if kind_of == "rectangle" and (x1 == x2 or y1 == y2):
                    raise ToolError(f"Rectangle {index} has zero width or height. Nothing was changed.")
                if kind_of == "line" and (x1, y1) == (x2, y2):
                    raise ToolError(f"Line {index} has zero length. Nothing was changed.")
                out.append({"type": kind_of, "x1": x1, "y1": y1, "x2": x2, "y2": y2})
            elif kind_of == "circle":
                out.append({
                    "type": "circle",
                    "cx": _finite(entity.get("cx"), f"entity {index} cx") * mm,
                    "cy": _finite(entity.get("cy"), f"entity {index} cy") * mm,
                    "radius": _positive(entity.get("radius"), f"entity {index} radius") * mm,
                })
            else:
                raise ToolError(f"Sketch entity {index} has type {kind_of!r}; use rectangle, circle or line. Nothing was changed.")
        return {"plane": plane, "entities": out}
    if kind == "extrude":
        sketch = spec.get("sketch_name")
        if not isinstance(sketch, str) or not sketch:
            raise ToolError("sketch_name must name the sketch to extrude. Nothing was changed.")
        operation = spec.get("operation", "NEW")
        if operation not in EXTRUDE_OPERATIONS:
            raise ToolError(f"operation must be one of {', '.join(EXTRUDE_OPERATIONS)}. Nothing was changed.")
        end = spec.get("end", "BLIND")
        if end not in END_BOUNDS:
            raise ToolError(f"end must be one of {', '.join(END_BOUNDS)}. Nothing was changed.")
        depth = _positive(spec.get("depth_mm"), "depth_mm") * mm if end == "BLIND" else 0.0
        return {"sketch_name": sketch, "operation": operation, "end": end, "depth_m": depth,
                "opposite_direction": bool(spec.get("opposite_direction"))}
    if kind in ("fillet", "chamfer"):
        points = spec.get("edge_points_mm")
        if not isinstance(points, list) or not points:
            raise ToolError("edge_points_mm must list one point on each edge, as [x, y, z] in millimetres. Nothing was changed.")
        checked = []
        for index, point in enumerate(points):
            if not isinstance(point, list) or len(point) != 3:
                raise ToolError(f"Edge point {index} must be [x, y, z] in millimetres. Nothing was changed.")
            checked.append([_finite(v, f"edge point {index}") * mm for v in point])
        size_key = "radius_mm" if kind == "fillet" else "distance_mm"
        return {"edge_points_m": checked, "size_m": _positive(spec.get(size_key), size_key) * mm}
    raise ToolError("kind must be sketch, extrude, fillet or chamfer. Nothing was changed.")


def integrity_compare(before: Dict[str, Any], after: Dict[str, Any], made: List[str]) -> Dict[str, Any]:
    """`clean` only when both snapshots were read and the feature names after are exactly the names
    before plus `made`. Bodies are recorded (an extrude adds one); a missing body list is not_checked."""
    if any(s.get(k) is None for s in (before, after) for k in ("features", "bodies")):
        return {"result": "not_checked", "detail": "A feature or body list could not be read.", "before": before, "after": after}
    before_names = [f["name"] for f in before["features"]]
    after_names = [f["name"] for f in after["features"]]
    unexpected = [n for n in after_names if n not in before_names and n not in made]
    missing = [n for n in before_names + list(made) if n not in after_names]
    if not unexpected and not missing and len(after_names) == len(before_names) + len(made):
        return {"result": "clean", "before": before, "after": after}
    return {
        "result": "foreign",
        "unexpected_features": unexpected,
        "missing_features": missing,
        "detail": "The feature list after the step is not the list before plus what this step made.",
        "before": before,
        "after": after,
    }


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


def _write_json_unlocked(path: Path, data: Any) -> None:
    """Write to a temporary file in the same folder, flush it to disk, then rename it over the
    target, so a crash leaves either the old file or the new one, never half of one. The caller
    holds the state lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json_atomically(path: Path, data: Any) -> None:
    with StateLock(path):
        _write_json_unlocked(path, data)


class ProtectedFiles:
    """What the bridge may and may not write, persisted so a restarted bridge keeps refusing.

    `protected`: base files read at the start of a run. Never written.
    `copies`: files this bridge made. The only files it writes.

    The file on disk is the record, and more than one bridge may be running. So every change
    is one transaction under the state lock: read the file, change it, write it back. Every
    write decision reads the file again first, so a bridge never acts on an older picture than
    another bridge left. Retirements never go away: one this bridge knows of is kept even if
    the file it reads does not have it yet.

    If the state file exists but cannot be read, nothing is written at all until a
    person fixes or removes it (fail closed): an empty list would silently drop
    the protection of every base recorded in it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.copies: Dict[str, Dict[str, Any]] = {}
        self.error: Optional[str] = None
        self.refresh()

    def _read(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        if not self.path.exists():
            return {}, {}
        data = json.loads(self.path.read_text("utf-8"))
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
        return protected, copies

    def refresh(self) -> None:
        """Read the file again. Keeps every protection and retirement this bridge already knows."""
        if self.error:
            return
        try:
            protected, copies = self._read()
        except (OSError, ValueError) as err:
            self.error = f"The bridge's state file {self.path} cannot be read ({err})."
            log(self.error + " Refusing every write until it is fixed or removed.")
            return
        for key, record in self.entries.items():
            protected.setdefault(key, record)
        for key, record in self.copies.items():
            if record.get("retired") and key in copies and not copies[key].get("retired"):
                copies[key]["retired"] = record["retired"]
        self.entries, self.copies = protected, copies

    def _require_readable(self) -> None:
        if self.error:
            raise ToolError(
                self.error + " It records which base files are protected, so the bridge writes nothing until "
                "a person checks it, then fixes or removes it. Nothing was changed."
            )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Read, change and write the state as one step under the lock."""
        self._require_readable()
        with StateLock(self.path):
            self.refresh()
            self._require_readable()
            yield
            _write_json_unlocked(self.path, {"format": STATE_FORMAT, "protected": self.entries, "copies": self.copies})

    def add(self, file_path: str, record: Dict[str, Any]) -> None:
        key = normalise(file_path)
        with self.transaction():
            if key in self.copies:
                raise ToolError(f"{file_path} is a copy this bridge made, not a base model.")
            self.entries[key] = record

    def register_copy(self, file_path: str, record: Dict[str, Any]) -> None:
        key = normalise(file_path)
        with self.transaction():
            if key in self.entries:
                raise ToolError(f"Refused: {file_path} is a protected base path.")
            self.copies[key] = record

    def update_copy(self, file_path: str, **fields: Any) -> None:
        """Set fields on a copy's record; a field set to None is removed."""
        key = normalise(file_path)
        with self.transaction():
            record = self.copies.get(key)
            if record is None:
                raise ToolError(f"{file_path} is not a copy this bridge made.")
            for name, value in fields.items():
                if value is None:
                    record.pop(name, None)
                else:
                    record[name] = value

    def retire_copy(self, file_path: str, reason: str) -> None:
        """Stop writing to a copy whose state is no longer known. The mark holds in this bridge
        even if it cannot be written to the file."""
        key = normalise(file_path)
        record = self.copies.get(key)
        if record is not None:
            record["retired"] = reason
        try:
            with self.transaction():
                if key in self.copies:
                    self.copies[key]["retired"] = reason
        except (OSError, ToolError) as err:
            log(f"Could not record the retired copy: {err}")

    def is_protected(self, file_path: str) -> bool:
        self.refresh()
        return normalise(file_path) in self.entries

    def is_copy(self, file_path: str) -> bool:
        self.refresh()
        return normalise(file_path) in self.copies

    def copy_record(self, file_path: str) -> Optional[Dict[str, Any]]:
        self.refresh()
        return self.copies.get(normalise(file_path))

    def check_writable(self, file_path: str) -> None:
        self.refresh()
        self._require_readable()
        if normalise(file_path) in self.entries:
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

    # bodies, faces and references -------------------------------------------

    def bodies(self, doc: Any) -> Optional[List[Dict[str, Any]]]:
        """Solid bodies by name, or None when SOLIDWORKS returns no answer (unknown, never empty)."""
        if doc.GetType() != SW_DOC_PART:
            return None
        try:
            found = doc.GetBodies2(SW_SOLID_BODY, False)
        except Exception as err:  # noqa: BLE001
            log(f"GetBodies2 raised: {err}")
            return None
        return [{"name": body.Name, "face_count": len(body.GetFaces() or [])} for body in (found or [])]

    def faces(self, doc: Any) -> List[Dict[str, Any]]:
        out = []
        for body in doc.GetBodies2(SW_SOLID_BODY, False) or []:
            faces = []
            for face in body.GetFaces() or []:
                surface = face.GetSurface()
                entry: Dict[str, Any] = {"area_m2": face.GetArea()}
                if surface.IsPlane():
                    params = list(surface.PlaneParams)  # normal xyz, root point xyz (metres)
                    entry.update(surface="PLANE", normal=params[0:3], origin_m=params[3:6])
                elif surface.IsCylinder():
                    params = list(surface.CylinderParams)  # origin xyz, axis xyz, radius (metres)
                    entry.update(surface="CYLINDER", origin_m=params[0:3], axis=params[3:6], radius_m=params[6])
                else:
                    entry["surface"] = "OTHER"
                faces.append(entry)
            out.append({"name": body.Name, "face_count": len(faces), "faces": faces})
        return out

    def external_reference_count(self, doc: Any) -> Optional[int]:
        """External file references SOLIDWORKS reports, or None when it cannot say."""
        for call in (lambda: doc.Extension.ListExternalFileReferenceCount(), lambda: doc.ListExternalFileReferencesCount(False)):
            try:
                count = call()
                if isinstance(count, int) and count >= 0:
                    return count
            except Exception as err:  # noqa: BLE001
                log(f"external reference count raised: {err}")
        return None

    def edges_at(self, doc: Any, point_m: List[float]) -> int:
        """How many solid-body edges pass within EDGE_TOLERANCE_M of a point."""
        x, y, z = point_m
        count = 0
        for body in doc.GetBodies2(SW_SOLID_BODY, False) or []:
            for edge in body.GetEdges() or []:
                closest = edge.GetClosestPointOn(x, y, z)
                if closest is None:
                    continue
                cx, cy, cz = list(closest)[0:3]
                if ((cx - x) ** 2 + (cy - y) ** 2 + (cz - z) ** 2) ** 0.5 <= EDGE_TOLERANCE_M:
                    count += 1
        return count

    # building ------------------------------------------------------------------

    def part_template(self) -> str:
        template = os.environ.get("SOLIDWORKS_PART_TEMPLATE") or self.app.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_PART)
        if not template or not str(template).lower().endswith(".prtdot") or not Path(str(template)).is_file():
            raise ToolError(
                f"No usable part template (got {template!r}). Set the default part template in SOLIDWORKS "
                "(System Options > Default Templates), or set SOLIDWORKS_PART_TEMPLATE to a .prtdot file. Nothing was created."
            )
        return str(template)

    def new_part(self, template: str) -> Any:
        doc = self.app.NewDocument(template, 0, 0, 0)
        if doc is None:
            raise ToolError("SOLIDWORKS did not create a new part from the template. Nothing was created.")
        return doc

    def save_as(self, doc: Any, new_path: str) -> Dict[str, Any]:
        errors, warnings = self._out_int(), self._out_int()
        ok = doc.Extension.SaveAs3(new_path, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_SILENT, None, None, errors, warnings)
        return {"ok": bool(ok), "errors": errors.value, "warnings": warnings.value}

    def import_step(self, path: str) -> Any:
        errors = self._out_int()
        doc = self.app.LoadFile4(path, "r", None, errors)
        if doc is None:
            raise ToolError(f"SOLIDWORKS could not import {path} (load error {errors.value}). Nothing was created.")
        return doc

    def plane_feature(self, doc: Any, plane: str) -> Any:
        """The reference plane named "<plane> Plane". None when the template names its planes otherwise:
        then the bridge cannot be sure which plane is which, and does not guess."""
        for feature in self._walk_features(doc):
            if feature.GetTypeName2() == "RefPlane" and feature.Name == f"{plane} Plane":
                return feature
        return None

    def sketch(self, doc: Any, plane: Any, entities: List[Dict[str, Any]]) -> None:
        doc.ClearSelection2(True)
        if not plane.Select2(False, 0):
            raise ToolError("SOLIDWORKS did not select the sketch plane.")
        manager = doc.SketchManager
        manager.AddToDB = True  # no snapping or inferencing: the coordinates go in as given
        try:
            manager.InsertSketch(True)
            for entity in entities:
                if entity["type"] == "rectangle":
                    made = manager.CreateCornerRectangle(entity["x1"], entity["y1"], 0, entity["x2"], entity["y2"], 0)
                elif entity["type"] == "circle":
                    made = manager.CreateCircleByRadius(entity["cx"], entity["cy"], 0, entity["radius"])
                else:
                    made = manager.CreateLine(entity["x1"], entity["y1"], 0, entity["x2"], entity["y2"], 0)
                if made is None:
                    raise ToolError(f"SOLIDWORKS did not create the sketch {entity['type']}.")
        finally:
            manager.InsertSketch(True)
            manager.AddToDB = False
            doc.ClearSelection2(True)

    def extrude(self, doc: Any, spec: Dict[str, Any]) -> Optional[str]:
        sketch = doc.FeatureByName(spec["sketch_name"])
        doc.ClearSelection2(True)
        if sketch is None or not sketch.Select2(False, 0):
            raise ToolError(f"SOLIDWORKS did not select the sketch {spec['sketch_name']}.")
        end = SW_END_BLIND if spec["end"] == "BLIND" else SW_END_THROUGH_ALL
        manager = doc.FeatureManager
        if spec["operation"] == "REMOVE":
            feature = manager.FeatureCut4(
                True, False, spec["opposite_direction"], end, 0, spec["depth_m"], 0, False, False, False, False, 0, 0,
                False, False, False, False, False, False, True, False, True, False, SW_START_SKETCH_PLANE, 0, False, False,
            )
        else:
            feature = manager.FeatureExtrusion3(
                True, False, spec["opposite_direction"], end, 0, spec["depth_m"], 0, False, False, False, False, 0, 0,
                False, False, False, False, spec["operation"] == "ADD", False, True, SW_START_SKETCH_PLANE, 0, False,
            )
        doc.ClearSelection2(True)
        return feature.Name if feature is not None else None

    def select_edges(self, doc: Any, points_m: List[List[float]]) -> None:
        doc.ClearSelection2(True)
        for x, y, z in points_m:
            if not doc.Extension.SelectByID2("", "EDGE", x, y, z, True, 0, None, 0):
                raise ToolError(f"SOLIDWORKS did not select the edge at {[x, y, z]} m.")
        selected = doc.SelectionManager.GetSelectedObjectCount2(-1)
        if selected != len(points_m):
            raise ToolError(f"SOLIDWORKS selected {selected} entities for {len(points_m)} edge points.")

    def fillet(self, doc: Any, spec: Dict[str, Any]) -> Optional[str]:
        self.select_edges(doc, spec["edge_points_m"])
        feature = doc.FeatureManager.FeatureFillet3(
            SW_FILLET_UNIFORM_RADIUS, spec["size_m"], 0, 0, SW_FILLET_TYPE_SIMPLE, SW_FILLET_OVERFLOW_DEFAULT, 0,
            None, None, None, None, None, None, None,
        )
        doc.ClearSelection2(True)
        return feature.Name if feature is not None else None

    def chamfer(self, doc: Any, spec: Dict[str, Any]) -> Optional[str]:
        self.select_edges(doc, spec["edge_points_m"])
        feature = doc.FeatureManager.InsertFeatureChamfer(0, SW_CHAMFER_EQUAL_DISTANCE, spec["size_m"], 0, spec["size_m"], 0, 0, 0)
        doc.ClearSelection2(True)
        return feature.Name if feature is not None else None

    def feature_face_count(self, doc: Any, name: str) -> Optional[int]:
        feature = doc.FeatureByName(name)
        if feature is None:
            return None
        count = feature.GetFaceCount()
        return count if isinstance(count, int) else None

    def delete_feature(self, doc: Any, name: str) -> bool:
        feature = doc.FeatureByName(name)
        doc.ClearSelection2(True)
        if feature is None or not feature.Select2(False, 0):
            return False
        return bool(doc.EditDelete())


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
        record = protected.copy_record(path) if doc.GetPathName() else None
        if rebuild_ok is False:
            stale[path] = "the last rebuild reported failure"
            if record is not None:
                try:
                    protected.update_copy(path, rebuild_failed=stale[path])
                except (OSError, ToolError) as err:
                    log(f"Could not record the failed rebuild: {err}")
        elif rebuild_ok is True:
            stale.pop(path, None)
            if record is not None and record.get("rebuild_failed"):
                protected.update_copy(path, rebuild_failed=None)
        elif record is not None and record.get("rebuild_failed"):
            # Recorded by this or an earlier run of the bridge: only a new successful rebuild clears it.
            stale[path] = record["rebuild_failed"]
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
        current = {d["name"]: dict(d) for d in session().dimensions(doc)} if dimension_values else {}  # a copy: the values before
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
                    undo.append(lambda n=name, v=before: restore_dimension(doc, n, v))
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
            # A restore SOLIDWORKS answers with a status rather than an exception is only believed once
            # the value reads back as it was. Every name this call touched is read, including the one that failed.
            not_undone += restoration_problems(doc, dimension_values, current, global_values, found_globals)
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
        try:
            health = health_of(doc, rebuild_ok)
        except Exception as err:  # noqa: BLE001 - the change is in, but its health could not be read
            reason = f"the health after a change could not be read ({err})"
            stale[normalise(args["expected_path"])] = reason
            try:
                protected.update_copy(args["expected_path"], rebuild_failed=reason)
            except (OSError, ToolError) as record_err:
                log(f"Could not record the unread health: {record_err}")
            raise ToolError(
                f"The changes were applied, but {reason}. Treat this variant as failed: do not measure or save it until "
                f"a rebuild reports success. Changes: {json.dumps(changes)}"
            ) from err
        return {"changes": changes, "health": health, "saved": False}

    def restore_dimension(doc: Any, name: str, value: float) -> None:
        status = session().set_dimension(doc, name, value)
        if status != "Successful":
            raise ToolError(f"SOLIDWORKS refused to restore {name}: {status}")

    def restoration_problems(
        doc: Any,
        dimension_values: Dict[str, Any],
        current: Dict[str, Dict[str, Any]],
        global_values: Dict[str, Any],
        found_globals: Dict[str, Dict[str, Any]],
    ) -> List[str]:
        """Read back every value a failed call touched. Anything not as it was, or unreadable, is a problem."""
        problems: List[str] = []
        try:
            now = {d["name"]: d.get("value_si") for d in session().dimensions(doc)} if dimension_values else {}
        except Exception as err:  # noqa: BLE001
            return [f"the dimensions could not be read back ({err})"]
        for name in dimension_values:
            was = current[name]["value_si"]
            value = now.get(name)
            if not isinstance(value, (int, float)) or abs(value - was) > 1e-9 * max(1.0, abs(was)):
                problems.append(f"{name} reads {value!r}, not {was!r}")
        for name in global_values:
            try:
                found = session().find_global(doc, name)
            except Exception as err:  # noqa: BLE001
                problems.append(f"{name} could not be read back ({err})")
                continue
            if found is None or " ".join(found["text"].split()) != " ".join(found_globals[name]["text"].split()):
                problems.append(f"{name} reads {None if found is None else found['text']!r}, not {found_globals[name]['text']!r}")
        return problems

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

    # -------------------------------------------------------------- new parts, STEP import, building

    def snapshot(doc: Any) -> Dict[str, Any]:
        try:
            features = session().feature_list(doc)
        except Exception as err:  # noqa: BLE001
            log(f"feature list failed: {err}")
            features = None
        return {
            "features": features,
            "sketches": [f["name"] for f in features if f["type"] == "ProfileFeature"] if features is not None else None,
            "bodies": session().bodies(doc),
        }

    def check_step_path(step_path: str) -> Path:
        source = Path(step_path)
        if source.suffix.lower() not in (".step", ".stp"):
            raise ToolError("step_path must be a .step or .stp file. Nothing was imported.")
        if not source.is_file():
            raise ToolError(f"No file at {step_path}. Nothing was imported.")
        size = source.stat().st_size
        if size == 0 or size > MAX_IMPORT_BYTES:
            raise ToolError(f"The file is {size} bytes; the bridge imports files from 1 byte to {MAX_IMPORT_BYTES} bytes. Nothing was imported.")
        return source

    def save_new(doc: Any, target: Path, record: Dict[str, Any]) -> Any:
        result = session().save_as(doc, str(target))
        if not result["ok"] or not target.exists():
            raise ToolError(f"SOLIDWORKS did not save the new part to {target} (errors {result['errors']}, warnings {result['warnings']}).")
        if normalise(doc.GetPathName() or "") != normalise(str(target)):
            raise ToolError(f"The new part was saved, but its document is at {doc.GetPathName()}, not {target}. Stopping before any write.")
        try:
            protected.register_copy(str(target), {**record, "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
        except OSError as err:
            raise ToolError(f"The part was saved to {target}, but the bridge could not record it ({err}), so it will not write to it.") from err
        return doc

    @server.tool(
        "sw_new_part",
        "Create a NEW part from the default part template, save it at a new path, and register it as a file the "
        "bridge may write: the document an estimating model is built in from a drawing or text (rfq-to-quote paths "
        "C2 and A). Refuses an existing path. Checks the new part holds no sketch and no body. Name it "
        "<inquiry>_L<line>_<part>_R<rev>.SLDPRT." + UNVERIFIED,
        {"new_path": PATH},
        ["new_path"],
        read_only=False,
    )
    def new_part(args: Dict[str, Any]) -> Dict[str, Any]:
        protected._require_readable()
        target = check_new_path(args["new_path"], protected)
        if target.suffix.upper() != ".SLDPRT":
            raise ToolError("new_path must end in .SLDPRT. Nothing was created.")
        template = session().part_template()
        doc = save_new(session().new_part(template), target, {"kind": "created", "template": template})
        contents = snapshot(doc)
        if contents["features"] is None or contents["sketches"] or contents["bodies"] is None or contents["bodies"]:
            protected.retire_copy(str(target), "a new part was not empty, or its contents could not be read")
            raise ToolError(f"The new part {target} is not empty, or its contents could not be read. The bridge will not build in it.")
        return {"path": str(target), "template": template, "contents": contents}

    @server.tool(
        "sw_list_contents",
        "List the active document's FeatureManager tree (sketches are type ProfileFeature), its solid bodies, and the "
        "number of external file references SOLIDWORKS reports. Read-only. Call before and after every build or "
        "adapt step: the integrity check." + UNVERIFIED,
    )
    def list_contents(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        count = session().external_reference_count(doc)
        return {
            "document": doc.GetPathName() or doc.GetTitle(),
            **snapshot(doc),
            "external_references": {
                "present": None if count is None else count > 0,
                "count": count,
                "basis": "The external file reference count SOLIDWORKS reports for this document (in-context and derived "
                "references). None means SOLIDWORKS gave no answer: record unknown.",
            },
            "integrity_note": INTEGRITY_NOTE,
        }

    @server.tool(
        "sw_read_faces",
        "Solid bodies and faces of the active part: each face's area, and for planes the normal, for cylinders the "
        "radius and axis (metres). For an imported STEP model, holes and pockets are read from these faces: each one "
        "is a reading to confirm, not a feature. Read-only." + UNVERIFIED,
    )
    def read_faces(_: Dict[str, Any]) -> Dict[str, Any]:
        doc = session().active_doc()
        if doc.GetType() != SW_DOC_PART:
            raise ToolError("The active document is not a part. Faces are read from parts only.")
        return {"document": doc.GetPathName() or doc.GetTitle(), "bodies": session().faces(doc), "units": "metres, square metres"}

    @server.tool(
        "sw_add_feature",
        "Add one feature to the active part, which must be a file this bridge made (sw_new_part, sw_save_copy_and_open "
        "or sw_import_step) at expected_path: kind sketch (spec: plane Front, Top or Right; entities of type rectangle "
        "{x1,y1,x2,y2}, circle {cx,cy,radius} or line {x1,y1,x2,y2}, in mm), extrude (spec: sketch_name, operation NEW, "
        "ADD or REMOVE for a cut or through hole, end BLIND with depth_mm or THROUGH_ALL, opposite_direction), fillet "
        "(spec: edge_points_mm, one [x,y,z] on each edge, radius_mm) or chamfer (spec: edge_points_mm, distance_mm). "
        "Checks the spec and, for fillet and chamfer, that each point is on exactly one edge, before changing anything. "
        "Afterwards the part must rebuild, a fillet or chamfer must own a face per edge, and the feature list must be "
        "the old one plus this feature; otherwise the feature is deleted again. Does not save." + UNVERIFIED,
        {"expected_path": PATH, "kind": {"type": "string", "enum": ["chamfer", "extrude", "fillet", "sketch"]}, "spec": {"type": "object"}},
        ["expected_path", "kind", "spec"],
        read_only=False,
    )
    def add_feature(args: Dict[str, Any]) -> Dict[str, Any]:
        doc = active_for_write(args["expected_path"])
        kind = args["kind"]
        spec = feature_spec(kind, args["spec"])  # the whole step is checked before anything changes
        if doc.GetType() != SW_DOC_PART:
            raise ToolError("The active document is not a part. Nothing was changed.")
        before = snapshot(doc)
        if before["features"] is None or before["bodies"] is None:
            raise ToolError("SOLIDWORKS did not list this part's features or bodies, so the step cannot be checked. Nothing was changed.")
        plane = None
        if kind == "sketch":
            plane = session().plane_feature(doc, spec["plane"])
            if plane is None:
                raise ToolError(f"No reference plane named \"{spec['plane']} Plane\" in this part, so the bridge cannot tell which plane is meant. Nothing was changed.")
        if kind == "extrude" and not any(f["name"] == spec["sketch_name"] and f["type"] == "ProfileFeature" for f in before["features"]):
            raise ToolError(f"No sketch named {spec['sketch_name']} in this part. Nothing was changed.")
        if kind in ("fillet", "chamfer"):
            problems = []
            for point in spec["edge_points_m"]:
                count = session().edges_at(doc, point)
                if count != 1:
                    problems.append(f"{[round(v * 1000, 4) for v in point]} mm: {count} edges")
            if problems:
                raise ToolError(f"Each edge point must lie on exactly one solid edge, so the right edges take the {kind}: {'; '.join(problems)}. Nothing was changed.")

        before_names = [f["name"] for f in before["features"]]

        def roll_back(reason: str, name: Optional[str]) -> None:
            try:
                now = [f["name"] for f in session().feature_list(doc)]
                added = [n for n in now if n not in before_names]
                for added_name in reversed(added if name is None else [name]):
                    if not session().delete_feature(doc, added_name):
                        raise ToolError(f"SOLIDWORKS did not delete {added_name}")
                rebuilt = session().rebuild(doc)
                if [f["name"] for f in session().feature_list(doc)] != before_names or not rebuilt:
                    raise ToolError("the part after the delete is not the part before")
                health_of(doc, True)  # a verified rebuild of the part as it was: clears a failure this step recorded
            except Exception as err:  # noqa: BLE001
                protected.retire_copy(args["expected_path"], f"a {kind} failed and could not be removed")
                raise ToolError(
                    f"{reason} Removing it did not succeed ({err}), so this part is in an unknown state and the bridge "
                    "will not write to it again. Discard it and start a new one. Nothing was saved."
                ) from err
            raise ToolError(f"{reason} The feature was deleted again, so the part is as it was before this call. Nothing was saved.")

        name: Optional[str] = None
        try:
            if kind == "sketch":
                session().sketch(doc, plane, spec["entities"])
            elif kind == "extrude":
                name = session().extrude(doc, spec)
            elif kind == "fillet":
                name = session().fillet(doc, spec)
            else:
                name = session().chamfer(doc, spec)
        except Exception as err:  # noqa: BLE001
            roll_back(f"SOLIDWORKS failed the {kind}: {err}.", None)
        try:
            rebuild_ok = session().rebuild(doc)
            after = snapshot(doc)
            added = [f["name"] for f in (after["features"] or []) if f["name"] not in before_names]
            if kind == "sketch":
                sketches = [f["name"] for f in (after["features"] or []) if f["name"] in added and f["type"] == "ProfileFeature"]
                name = sketches[0] if len(sketches) == 1 else None
            if name is None or name not in added:
                roll_back(f"SOLIDWORKS did not report the new {kind}.", None)
            health = health_of(doc, rebuild_ok)
            if not health["regenerated"]:
                roll_back(f"The part did not rebuild cleanly after the {kind}.", name)
            faces_made = None
            if kind in ("fillet", "chamfer"):
                faces_made = session().feature_face_count(doc, name)
                if faces_made is None or faces_made < len(spec["edge_points_m"]):
                    roll_back(f"The {kind} owns {'an unknown number of' if faces_made is None else faces_made} face(s) for {len(spec['edge_points_m'])} edge(s), so not every edge took it.", name)
            integrity = integrity_compare(before, after, [name])
            if integrity["result"] == "foreign":
                protected.retire_copy(args["expected_path"], "unexpected features appeared during a build step")
                raise ToolError(
                    "The feature list after this step holds features this step did not make. The step fails; the bridge will "
                    f"not write to this part again. {json.dumps({k: integrity[k] for k in ('unexpected_features', 'missing_features')})}"
                )
        except ToolError:
            raise
        except Exception as err:  # noqa: BLE001 - the feature is in, but a check stopped: take it out again
            roll_back(f"Checking the new {kind} stopped unexpectedly ({err}).", None)
        return {"feature": name, "kind": kind, "faces_made": faces_made, "health": health, "document_integrity": integrity, "saved": False}

    @server.tool(
        "sw_import_step",
        "Import a customer's STEP file (rfq-to-quote path C1) as a part, save it at a NEW .SLDPRT path, and register "
        "that file as one the bridge may write. The STEP file is read, never changed. Refuses a STEP that opens as an "
        "assembly. Nothing is built on the imported bodies: read them with sw_read_faces." + UNVERIFIED,
        {"step_path": PATH, "new_path": PATH},
        ["step_path", "new_path"],
        read_only=False,
    )
    def import_step(args: Dict[str, Any]) -> Dict[str, Any]:
        protected._require_readable()
        source = check_step_path(args["step_path"])
        target = check_new_path(args["new_path"], protected)
        if target.suffix.upper() != ".SLDPRT":
            raise ToolError("new_path must end in .SLDPRT. Nothing was imported.")
        digest = file_fingerprint(source)["sha256"]
        doc = session().import_step(str(source))
        if doc.GetType() != SW_DOC_PART:
            raise ToolError(
                "The STEP file opened as an assembly, not a part. The bridge imports single parts only; ask the engineer "
                "for the part, or read the file as text, unverified. Nothing was saved."
            )
        save_new(doc, target, {"kind": "imported", "source_file": str(source), "source_sha256": digest})
        contents = snapshot(doc)
        if not contents["bodies"]:
            protected.retire_copy(str(target), "the import produced no solid body")
            raise ToolError("The import finished but SOLIDWORKS lists no solid body. Treat the import as failed.")
        return {"path": str(target), "source": str(source), "source_sha256": digest, **contents,
                "origin": "customer_step", "next": "Read faces with sw_read_faces; build nothing on these bodies."}

    return server


def main() -> None:
    default_state = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".manufacturing-solidworks" / "protected.json"
    state_path = Path(os.environ.get("SOLIDWORKS_BRIDGE_STATE", str(default_state)))
    build_server(SolidWorksSession, ProtectedFiles(state_path)).serve()


if __name__ == "__main__":
    main()
