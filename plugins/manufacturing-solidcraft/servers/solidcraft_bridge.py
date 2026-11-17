#!/usr/bin/env python3
"""SolidCraft bridge: the CAD adapter surface of manufacturing-solidcraft, over MCP stdio.

Implements the operations `skills/design-space-explore/reference/cad-adapter.md`
lists (variants, open-apply-save-as-new, building a part from a drawing, STEP
import, and the integrity check) on top of SolidCraft's own MCP server,
`solidcraft-cli mcp`, which it starts headless and talks to over stdio. The
design lives inside that process: nothing the engineer has open in the
SolidCraft app is reached, and the bridge writes a file only when it saves a
copy, a new part or an imported part it made. It never saves to a base model.
CAM is not implemented: SolidCraft has none.

Needs: SolidCraft installed (Windows, macOS or Linux) and Python 3.10+. Nothing else:
the MCP layer on both sides is standard library.

    SOLIDCRAFT_CLI            path to solidcraft-cli (default: found on PATH, or the
                              Windows install folder)
    SOLIDCRAFT_ALLOW_PATHS    folders SolidCraft may read and write, separated by the
                              system's path separator (; on Windows, : elsewhere);
                              passed to solidcraft-cli as --allow-path
    SOLIDCRAFT_BRIDGE_STATE   file that remembers protected base files and the copies
                              the bridge made (default ~/.manufacturing-solidcraft/protected.json)

Status: run against SolidCraft 0.44.0's headless engine (commit a40f6d8) on generated
test parts, by tools/cad-variants/tests/test_solidcraft_live.py. NOT yet run on a
plant's own models, against the SolidCraft desktop app, or on Windows or macOS.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_stdio import Server, ToolError, log  # noqa: E402

VERSION = "0.1.0"
UNVERIFIED = (
    " (Run only against SolidCraft 0.44's headless engine on test parts; not yet on a plant's own models.)"
)
TESTED_SOLIDCRAFT = "0.44.0"
DESIGN_SUFFIXES = (".sc",)
STEP_SUFFIXES = (".step", ".stp")
MAX_IMPORT_BYTES = 200 * 1024 * 1024
EDGE_TOLERANCE_MM = 0.01
# The plugin's plane names, as SolidCraft sketch planes. SolidCraft is Z-up. With these, sketch
# x and y run along: Front +X, +Z (extrudes toward -Y); Top +X, +Y (toward +Z); Right +Y, +Z
# (toward +X). Checked against SolidCraft 0.44 by the live test.
PLANES = {
    "Front": {"plane": "XZ", "flip": True},
    "Top": {"plane": "XY"},
    "Right": {"plane": "YZ"},
}
PLANE_AXES = {
    "Front": "sketch x = world +X, sketch y = world +Z, extrudes toward -Y",
    "Top": "sketch x = world +X, sketch y = world +Y, extrudes toward +Z",
    "Right": "sketch x = world +Y, sketch y = world +Z, extrudes toward +X",
}
EXTRUDE_OPERATIONS = {"NEW": "new", "ADD": "join", "REMOVE": "cut"}
END_BOUNDS = ("BLIND", "THROUGH_ALL")


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without SolidCraft)
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


STATE_FORMAT = 2
INTEGRITY_NOTE = (
    "Use sc_list_contents before and after a step for the integrity check: it lists the timeline (features "
    "and sketches), the bodies, and the components. Record the check as clean only when both reads succeeded "
    "and the after-list holds exactly the before-list plus what the step made."
)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ToolError(f"{label} must be a finite number in millimetres, not {value!r}. Nothing was changed.")
    return float(value)


def _positive(value: Any, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ToolError(f"{label} must be greater than zero, not {value!r}. Nothing was changed.")
    return number


def _name(spec: Dict[str, Any], key: str = "name") -> Optional[str]:
    value = spec.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(c in value for c in "|,~#]\n"):
        raise ToolError(f"{key} must be a short name without | , ~ # ] or a line break. Nothing was changed.")
    return value.strip()


def feature_spec(kind: str, spec: Any) -> Dict[str, Any]:
    """Check a build step completely, in millimetres, before anything is changed."""
    if not isinstance(spec, dict):
        raise ToolError("spec must be an object. Nothing was changed.")
    if kind == "sketch":
        plane = spec.get("plane")
        if plane not in PLANES:
            raise ToolError(f"plane must be one of {', '.join(PLANES)}. Nothing was changed.")
        entities = spec.get("entities")
        if not isinstance(entities, list) or not entities:
            raise ToolError("A sketch needs at least one entity (rectangle, circle or line). Nothing was changed.")
        if len(entities) > 500:
            raise ToolError("A sketch takes at most 500 entities per call. Nothing was changed.")
        out = []
        for index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                raise ToolError(f"Sketch entity {index} is not an object. Nothing was changed.")
            kind_of = entity.get("type")
            if kind_of in ("rectangle", "line"):
                x1, y1, x2, y2 = (_finite(entity.get(k), f"entity {index} {k}") for k in ("x1", "y1", "x2", "y2"))
                if kind_of == "rectangle" and (x1 == x2 or y1 == y2):
                    raise ToolError(f"Rectangle {index} has zero width or height. Nothing was changed.")
                if kind_of == "line" and (x1, y1) == (x2, y2):
                    raise ToolError(f"Line {index} has zero length. Nothing was changed.")
                out.append({"type": kind_of, "x1": x1, "y1": y1, "x2": x2, "y2": y2})
            elif kind_of == "circle":
                out.append({
                    "type": "circle",
                    "cx": _finite(entity.get("cx"), f"entity {index} cx"),
                    "cy": _finite(entity.get("cy"), f"entity {index} cy"),
                    "radius": _positive(entity.get("radius"), f"entity {index} radius"),
                })
            else:
                raise ToolError(f"Sketch entity {index} has type {kind_of!r}; use rectangle, circle or line. Nothing was changed.")
        return {"plane": plane, "entities": out, "name": _name(spec)}
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
        depth = _positive(spec.get("depth_mm"), "depth_mm") if end == "BLIND" else None
        return {"sketch_name": sketch, "operation": operation, "end": end, "depth_mm": depth,
                "opposite_direction": bool(spec.get("opposite_direction")), "name": _name(spec)}
    if kind in ("fillet", "chamfer"):
        points = spec.get("edge_points_mm")
        if not isinstance(points, list) or not points:
            raise ToolError("edge_points_mm must list one point on each edge, as [x, y, z] in millimetres. Nothing was changed.")
        if len(points) > 200:
            raise ToolError("At most 200 edges per call. Nothing was changed.")
        checked = []
        for index, point in enumerate(points):
            if not isinstance(point, list) or len(point) != 3:
                raise ToolError(f"Edge point {index} must be [x, y, z] in millimetres. Nothing was changed.")
            checked.append([_finite(v, f"edge point {index}") for v in point])
        size_key = "radius_mm" if kind == "fillet" else "distance_mm"
        return {"edge_points_mm": checked, "size_mm": _positive(spec.get(size_key), size_key), "name": _name(spec)}
    raise ToolError("kind must be sketch, extrude, fillet or chamfer. Nothing was changed.")


def point_segment_distance(point: List[float], start: List[float], end: List[float]) -> float:
    d = [e - s for s, e in zip(start, end)]
    length2 = sum(c * c for c in d)
    if length2 == 0:
        return math.dist(point, start)
    t = max(0.0, min(1.0, sum((p - s) * c for p, s, c in zip(point, start, d)) / length2))
    return math.dist(point, [s + t * c for s, c in zip(start, d)])


def edges_at(point: List[float], edges: List[Dict[str, Any]], tolerance: float = EDGE_TOLERANCE_MM) -> List[Dict[str, Any]]:
    """The edges a point lies on. A straight edge (its length equals the distance between its ends)
    matches anywhere along it; a curved edge only at the midpoint SolidCraft gives for it, because
    its curve is not in the topology read."""
    found = []
    for edge in edges:
        start, end, mid = edge.get("start"), edge.get("end"), edge.get("mid")
        straight = start is not None and end is not None and abs(math.dist(start, end) - float(edge.get("length", -1))) < 1e-6
        if straight and point_segment_distance(point, start, end) <= tolerance:
            found.append(edge)
        elif mid is not None and math.dist(point, mid) <= tolerance:
            found.append(edge)
    return found


def integrity_compare(before: Dict[str, Any], after: Dict[str, Any], made: List[str]) -> Dict[str, Any]:
    """`clean` only when both snapshots were read and the feature names after are exactly the names
    before plus `made`. Bodies are recorded; a missing body list is not_checked."""
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


def sketch_commands(spec: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    """The SolidCraft batch for one sketch: create it on the plane, draw, finish."""
    commands = [{"command": "sketch.create", "params": dict(PLANES[spec["plane"]], name=name)}]
    for entity in spec["entities"]:
        if entity["type"] == "rectangle":
            commands.append({"command": "sketch.rectangle.two_point",
                             "params": {"p0": [entity["x1"], entity["y1"]], "p1": [entity["x2"], entity["y2"]]}})
        elif entity["type"] == "line":
            commands.append({"command": "sketch.line",
                             "params": {"points": [[entity["x1"], entity["y1"]], [entity["x2"], entity["y2"]]]}})
        else:
            commands.append({"command": "sketch.circle.center",
                             "params": {"center": [entity["cx"], entity["cy"]], "radius": entity["radius"]}})
    commands.append({"command": "sketch.finish", "params": {}})
    return commands


def extrude_params(spec: Dict[str, Any], name: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {"sketch": spec["sketch_name"], "operation": EXTRUDE_OPERATIONS[spec["operation"]], "name": name}
    if spec["end"] == "THROUGH_ALL":
        params["through_all"] = True
        # A through cut from a sketch plane inside the part goes both ways, as a through hole should.
        params["direction"] = "symmetric" if spec["operation"] == "REMOVE" else ("negative" if spec["opposite_direction"] else "positive")
    else:
        params["distance"] = spec["depth_mm"]
        params["direction"] = "negative" if spec["opposite_direction"] else "positive"
    return params


def cli_command() -> List[str]:
    """How to start SolidCraft's MCP server: headless, with the folders it may use."""
    cli = os.environ.get("SOLIDCRAFT_CLI") or shutil.which("solidcraft-cli")
    if not cli:
        windows = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SolidCraft" / "solidcraft-cli.exe"
        cli = str(windows) if windows.exists() else shutil.which("solidcraft.cli")  # the snap's name for it
    if not cli:
        raise ToolError(
            "solidcraft-cli was not found. Install SolidCraft, or set SOLIDCRAFT_CLI to the full path of "
            "solidcraft-cli (Flatpak: a wrapper script that runs `flatpak run --command=solidcraft-cli "
            "io.tangencylabs.solidcraft \"$@\"`). Nothing was changed."
        )
    argv = [cli, "mcp", "--headless", "--param-check", "strict"]
    folders = [f for f in os.environ.get("SOLIDCRAFT_ALLOW_PATHS", "").split(os.pathsep) if f.strip()]
    for folder in folders:
        argv += ["--allow-path", folder.strip()]
    if folders:
        argv.append("--require-allow-path")
    return argv


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
                "to a copy, never the base. Use sc_save_copy_and_open first."
            )
        record = self.copies.get(normalise(file_path))
        if record is None:
            raise ToolError(
                f"Refused: {file_path} is not a copy this bridge made. The bridge writes only to a copy made "
                "with sc_save_copy_and_open, never to a file it did not create. Nothing was changed."
            )
        if record.get("retired"):
            raise ToolError(
                f"Refused: the copy {file_path} was left in an unknown state ({record['retired']}). Discard it and "
                "make a new copy. Nothing was changed."
            )


# ---------------------------------------------------------------------------
# The SolidCraft MCP server, as a client
# ---------------------------------------------------------------------------


class OutcomeUnknown(ToolError):
    """SolidCraft stopped answering during a call: whether the call took effect is not known."""


class SolidCraftClient:
    """`solidcraft-cli mcp --headless`, started by the bridge and spoken to over stdio.

    Headless means the design lives inside that process: nothing the engineer has open in the
    SolidCraft app is reached, and a design is on disk only once the bridge saves it."""

    def __init__(self, argv: List[str], timeout: float = 600.0) -> None:
        import queue
        import subprocess
        import threading

        self.argv = argv
        self.timeout = timeout
        self.next_id = 0
        try:
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as err:
            raise ToolError(
                f"Could not start {argv[0]} ({err}). Install SolidCraft, or set SOLIDCRAFT_CLI to the full path of "
                "solidcraft-cli. Nothing was changed."
            ) from err
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump(stream: Any, sink: Optional["queue.Queue[Optional[str]]"]) -> None:
            for line in stream:
                if sink is not None:
                    sink.put(line)
                else:
                    log("solidcraft-cli: " + line.rstrip())
            if sink is not None:
                sink.put(None)

        threading.Thread(target=pump, args=(self.process.stdout, self.lines), daemon=True).start()
        threading.Thread(target=pump, args=(self.process.stderr, None), daemon=True).start()
        self.info = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "manufacturing-solidcraft-bridge", "version": VERSION},
        }, timeout=60.0)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def alive(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        if self.alive():
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - shutting down
                self.process.kill()
                with contextlib.suppress(Exception):
                    self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            with contextlib.suppress(Exception):
                if stream is not None:
                    stream.close()

    def _send(self, message: Dict[str, Any]) -> None:
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as err:
            raise OutcomeUnknown(f"SolidCraft stopped accepting requests ({err}).") from err

    def _request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        import queue
        import time

        self.next_id += 1
        ident = self.next_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OutcomeUnknown(f"SolidCraft did not answer {method} in time.")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise OutcomeUnknown(f"SolidCraft exited during {method} (exit code {self.process.poll()}).")
            try:
                message = json.loads(line)
            except ValueError:
                log("solidcraft-cli (not JSON): " + line.rstrip())
                continue
            if not isinstance(message, dict) or message.get("id") != ident:
                continue  # a notification, or an answer to something else
            if "error" in message:
                error = message["error"] or {}
                raise ToolError(f"SolidCraft refused {method}: {error.get('message', error)}")
            return message.get("result")

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Tuple[Any, bool]:
        """One tool call. Returns (value, is_error): value is the first text block, parsed as JSON
        where it is JSON, and is_error is the server's isError flag."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        is_error = bool(result.get("isError"))
        if not texts:
            return None, is_error
        try:
            return json.loads(texts[-1] if is_error and len(texts) > 1 else texts[0]), is_error
        except ValueError:
            return texts[0], is_error


class Refused(ToolError):
    """SolidCraft refused a call. SolidCraft documents that a refused command leaves the design
    unchanged; the bridge still reads the design back after every refused write."""


class SolidCraftSession:
    """The calls the bridge makes, on top of SolidCraft's MCP tools. Tests replace this class."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _ok(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        value, is_error = self.client.call(tool, arguments or {})
        if is_error:
            raise Refused(f"SolidCraft refused {tool}: {value if isinstance(value, str) else json.dumps(value)[:600]}")
        return value

    def close(self) -> None:
        self.client.close()

    def version(self) -> Dict[str, Any]:
        return self._ok("version")

    def inspect(self) -> Dict[str, Any]:
        return self._ok("inspect_design", {"measure": False, "sketches": True, "joints": False,
                                           "timeline": "summary", "bodies": "names"})

    def parameters(self) -> List[Dict[str, Any]]:
        return self._ok("execute", {"command": "parameters.list", "params": {}})["parameters"]

    def components(self) -> Dict[str, Any]:
        return self._ok("execute", {"command": "component.list", "params": {}})

    def measure(self) -> Dict[str, Any]:
        return self._ok("measure", {"summary": True})

    def topology(self, body: str) -> Dict[str, Any]:
        return self._ok("body_topology", {"body": body})

    def cylinders(self, body: str) -> List[Dict[str, Any]]:
        return self._ok("body_topology", {"body": body, "faces": "cylinders"}).get("cylinders") or []

    def open(self, path: str) -> Dict[str, Any]:
        return self._ok("open", {"path": path})

    def save(self, path: str) -> Dict[str, Any]:
        return self._ok("save", {"path": path})

    def new_design(self, name: str) -> Dict[str, Any]:
        return self._ok("new_design", {"name": name})

    def check_parameters(self, values: Dict[str, Any]) -> Tuple[Any, bool]:
        return self.client.call("check_parameters", {"set": values, "report": ["errors"]})

    def change_parameters(self, values: Dict[str, Any]) -> Dict[str, Any]:
        return self._ok("execute", {"command": "parameters.change", "params": {"changes": values}})

    def checkpoint(self) -> int:
        return int(self._ok("execute", {"command": "edit.checkpoint", "params": {}})["checkpoint"])

    def restore(self, checkpoint: int) -> bool:
        result = self._ok("execute", {"command": "edit.restore_checkpoint", "params": {"checkpoint": checkpoint}})
        return result.get("restored") is True

    def batch(self, commands: List[Dict[str, Any]]) -> Dict[str, Any]:
        value, is_error = self.client.call("batch", {"commands": commands, "rollback": True, "verbose": False})
        if is_error and not isinstance(value, dict):
            raise Refused(f"SolidCraft refused the batch: {value}")
        return value

    def execute(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._ok("execute", {"command": command, "params": params, "verbose": False})


def start_session() -> SolidCraftSession:
    return SolidCraftSession(SolidCraftClient(cli_command()))


# ---------------------------------------------------------------------------
# The bridge's tools
# ---------------------------------------------------------------------------

PATH = {"type": "string", "minLength": 1}


def check_new_path(new_path: str, protected: ProtectedFiles) -> Path:
    target = Path(new_path)
    if not target.is_absolute():
        raise ToolError("new_path must be an absolute path, e.g. /home/eng/Estimates/EH-RFQ-0912_L1_PN-4471-B_R2.sc")
    if target.suffix.lower() not in DESIGN_SUFFIXES:
        raise ToolError("new_path must end in .sc, SolidCraft's design file.")
    if target.exists() or Path(str(target) + ".bak").exists():
        raise ToolError(f"Refused: {new_path} (or its .bak) already exists. This connector never overwrites a file.")
    if protected.is_protected(new_path):
        raise ToolError(f"Refused: {new_path} is a protected base path.")
    if not target.parent.exists():
        raise ToolError(f"The folder {target.parent} does not exist. Create it or choose another.")
    return target


def build_server(session_factory: Callable[[], Any], protected: ProtectedFiles) -> Server:
    server = Server(
        name="solidcraft-bridge",
        version=VERSION,
        instructions=(
            "CAD adapter for manufacturing-solidcraft. Read cad-adapter.md before the first call. The bridge runs "
            "SolidCraft headless and opens files itself: start from a base with sc_open_base, then make a copy with "
            "sc_save_copy_and_open, or start a new part with sc_new_part. It writes only files it made."
        ),
    )
    # The design open in the headless session: its path and what it is to the bridge.
    state: Dict[str, Any] = {"session": None, "path": None, "role": None, "base": None}

    def session() -> Any:
        if state["session"] is None:
            state["session"] = session_factory()
        return state["session"]

    def lost(err: Exception) -> None:
        """SolidCraft stopped answering. The design in its memory is gone with it; files on disk are
        as the bridge last saved them, which sc_open_copy checks before going on."""
        if state["session"] is not None:
            with contextlib.suppress(Exception):
                state["session"].close()
        state.update(session=None, path=None, role=None, base=None)
        log(f"SolidCraft session lost: {err}")

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except OutcomeUnknown as err:
                lost(err)
                raise ToolError(
                    f"{err} SolidCraft was stopped, and the design it held is gone with it. Files on disk are as the "
                    "bridge last saved them: reopen a copy with sc_open_copy (it checks the file first), or start "
                    "again from the base. Changes not yet saved with sc_save_active are lost."
                ) from err
        run.__name__ = fn.__name__
        return run

    def tool(name: str, description: str, properties: Optional[Dict[str, Any]] = None,
             required: Optional[List[str]] = None, read_only: bool = True) -> Callable[..., Any]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            return server.tool(name, description + UNVERIFIED, properties, required, read_only)(guarded(fn))
        return register

    def snapshot() -> Dict[str, Any]:
        info = session().inspect()
        timeline = info.get("timeline")
        bodies = info.get("bodies")
        features = None
        if isinstance(timeline, list):
            features = [{k: f.get(k) for k in ("id", "name", "type", "error", "warning", "suppressed", "rolled_back") if f.get(k) is not None}
                        for f in timeline]
        return {
            "path": info.get("path"),
            "dirty": info.get("dirty"),
            "features": features,
            "sketches": [s.get("name") for s in info.get("sketches") or []],
            "bodies": list(bodies) if isinstance(bodies, list) else None,
            "parameters": info.get("params"),
        }

    def failed_features(snap: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        features = snap.get("features") or []
        return ([f for f in features if f.get("error")], [f for f in features if f.get("warning")])

    def require_current(expected_path: str) -> Dict[str, Any]:
        """The design open in the session must be the copy named, and the bridge must have made it."""
        if not state["path"] or normalise(state["path"]) != normalise(expected_path):
            raise ToolError(
                f"Refused: the design open in the bridge's session is {state['path'] or 'none'}, not {expected_path}. "
                "Nothing was changed."
            )
        protected.check_writable(expected_path)
        snap = snapshot()
        if not snap["path"] or normalise(snap["path"]) != normalise(expected_path):
            raise ToolError(
                f"Refused: SolidCraft reports the open design as {snap['path'] or 'unsaved'}, not {expected_path}. "
                "Nothing was changed."
            )
        return snap

    def retire(path: str, reason: str) -> None:
        protected.retire_copy(path, reason)

    def back_to(checkpoint: int, before: Dict[str, Any], path: str, why: str) -> Dict[str, Any]:
        """Undo a write with the checkpoint taken before it, then read the design back. Anything other
        than the design as it was retires the copy."""
        try:
            restored = session().restore(checkpoint)
            after = snapshot()
        except Refused as err:
            retire(path, f"{why}; restoring the checkpoint failed ({err})")
            return {"restored": False, "detail": str(err)}
        same = (
            restored
            and [f["name"] for f in after["features"] or []] == [f["name"] for f in before["features"] or []]
            and after["bodies"] == before["bodies"]
            and after["parameters"] == before["parameters"]
        )
        if not same:
            retire(path, f"{why}; after restoring, the design does not read back as it was")
            return {"restored": False, "before": before, "after": after}
        return {"restored": True}

    # -- reading ----------------------------------------------------------------------------

    @tool("sc_status", "Check the connection: SolidCraft's version, and the design open in the bridge's headless session, if any. Read-only.")
    def status(_: Dict[str, Any]) -> Dict[str, Any]:
        version = session().version()
        result = {
            "solidcraft": version,
            "tested_with": TESTED_SOLIDCRAFT,
            "open_design": {"path": state["path"], "role": state["role"]} if state["path"] else None,
            "protected_files": len(protected.entries),
            "bridge_copies": len(protected.copies),
            "state_error": protected.error,
        }
        if version.get("version") != TESTED_SOLIDCRAFT:
            result["version_note"] = (
                f"This bridge was tested with SolidCraft {TESTED_SOLIDCRAFT}; this is {version.get('version')}. "
                "Say so on every output until it has been checked with this version."
            )
        return result

    @tool("sc_read_active_document", "Path, role (base, copy, new part, import), unsaved-changes flag, timeline, bodies and parameters of the design open in the bridge's session. Read-only.")
    def read_active(_: Dict[str, Any]) -> Dict[str, Any]:
        if not state["path"]:
            raise ToolError("No design is open in the bridge's session. Open a base with sc_open_base, or reopen a copy with sc_open_copy.")
        snap = snapshot()
        snap["role"] = state["role"]
        snap["protected"] = protected.is_protected(state["path"])
        snap["base"] = state["base"]
        return snap

    @tool("sc_read_parameters", "Every parameter of the open design: user parameters, sketch dimensions and feature inputs, each with its expression, value (mm or degrees), unit, which parameters it uses, which use it, and whether it reaches geometry. Read-only.")
    def read_parameters(_: Dict[str, Any]) -> Dict[str, Any]:
        if not state["path"]:
            raise ToolError("No design is open in the bridge's session.")
        rows = session().parameters()
        by_name = {r["name"]: r for r in rows}

        def reaches(name: str, seen: frozenset = frozenset()) -> bool:
            row = by_name.get(name)
            if row is None or name in seen:
                return False
            if row.get("source") in ("sketch", "feature"):
                return True
            return any(u.startswith("Feature:") or reaches(u, seen | {name}) for u in row.get("used_by") or [])

        out = []
        for row in rows:
            entry = {k: row.get(k) for k in ("name", "source", "expression", "value", "unit", "uses", "used_by", "error", "feature", "input", "comment")}
            entry["drives_geometry"] = reaches(row["name"])
            entry["settable"] = not row.get("uses") and not row.get("error") and row.get("unit") != "text"
            if row.get("uses"):
                entry["note"] = "Its value comes from other parameters: vary those instead. Setting it would break the link."
            out.append(entry)
        return {"path": state["path"], "parameters": out, "units": "mm and degrees"}

    @tool("sc_read_properties", "Regeneration health (every timeline feature's error and warning), then volume, bounding box and body count. A design with a failed feature is not measured. Read-only.")
    def read_properties(_: Dict[str, Any]) -> Dict[str, Any]:
        if not state["path"]:
            raise ToolError("No design is open in the bridge's session.")
        snap = snapshot()
        failed, warned = failed_features(snap)
        health = {"regenerated": not failed and snap["features"] is not None, "failed_features": failed, "warning_features": warned}
        if not health["regenerated"]:
            return {"path": state["path"], "health": health, "properties": None,
                    "note": "A feature failed to rebuild (or the timeline could not be read): this variant failed. Do not measure it."}
        measured = session().measure()
        return {
            "path": state["path"],
            "health": health,
            "unsaved_changes": snap["dirty"],
            "properties": {
                "volume_mm3": (measured.get("total") or {}).get("volume_mm3"),
                "body_count": measured.get("body_count"),
                "bodies": measured.get("bodies"),
                "open_bodies_skipped": (measured.get("total") or {}).get("open_bodies_skipped"),
            },
            "mass_note": "Mass is not read: it depends on the material assigned, which this bridge does not check. Multiply the volume by the plant's density for the material.",
        }

    @tool("sc_list_contents", "The integrity check: the timeline (features and sketches, in order, with any error), the bodies, and the components of the open design. " + INTEGRITY_NOTE + " Read-only.")
    def list_contents(_: Dict[str, Any]) -> Dict[str, Any]:
        if not state["path"]:
            raise ToolError("No design is open in the bridge's session.")
        snap = snapshot()
        external: Optional[bool]
        try:
            components = session().components().get("components") or []
            subcomponents = [c.get("name") for c in components if c.get("id") != 0]
            if subcomponents:
                external = None
                note = (f"The design holds components ({', '.join(map(str, subcomponents))}). SolidCraft does not report whether "
                        "a component is linked to another file, so external references are unknown.")
            else:
                external = False
                note = "The design holds no components, so nothing in it can point at another file."
        except Refused as err:
            subcomponents, external, note = None, None, f"Components could not be read ({err})."
        return {
            "path": snap["path"],
            "features": snap["features"],
            "sketches": snap["sketches"],
            "bodies": snap["bodies"],
            "components": subcomponents,
            "external_references_present": external,
            "external_references_note": note,
        }

    @tool("sc_read_faces", "Bodies, faces and edges of the open design: each face's area, centroid, a point on it, and whether it is a plane (with its normal) or a cylinder (radius, axis, origin); each edge's ends, midpoint and length. A cylinder is how a hole or boss is recognised. Read-only.")
    def read_faces(_: Dict[str, Any]) -> Dict[str, Any]:
        if not state["path"]:
            raise ToolError("No design is open in the bridge's session.")
        snap = snapshot()
        bodies = []
        for body in snap["bodies"] or []:
            topo = session().topology(body)
            cylinder_of = {}
            for cylinder in session().cylinders(body):
                for face_name in cylinder.get("names") or []:
                    cylinder_of[face_name] = {k: cylinder.get(k) for k in ("radius", "axis", "origin")}
            faces = []
            for face in topo.get("faces") or []:
                entry = {k: face.get(k) for k in ("index", "name", "area", "centroid", "point")}
                if face.get("name") in cylinder_of:
                    entry["surface"] = "cylinder"
                    entry["cylinder"] = cylinder_of[face["name"]]
                elif face.get("plane_normal") is not None:
                    entry["surface"] = "plane"
                    entry["normal"] = face["plane_normal"]
                else:
                    entry["surface"] = "other"
                faces.append(entry)
            edges = [{k: e.get(k) for k in ("index", "start", "end", "mid", "length")} for e in topo.get("edges") or []]
            bodies.append({"name": body, "faces": faces, "edges": edges})
        return {"path": snap["path"], "units": "mm", "bodies": bodies}

    # -- the base ---------------------------------------------------------------------------

    @tool(
        "sc_open_base",
        "Open an existing .sc design as a base model, in the bridge's own headless SolidCraft session, and protect "
        "it in the same call: record its SHA-256, size and modified time, and refuse every write to it from now on. "
        "The bridge never saves to a base. Make a copy with sc_save_copy_and_open before changing anything.",
        {"path": PATH}, ["path"], read_only=False,
    )
    def open_base(args: Dict[str, Any]) -> Dict[str, Any]:
        path = Path(args["path"])
        if not path.is_absolute() or path.suffix.lower() not in DESIGN_SUFFIXES:
            raise ToolError("path must be the absolute path of a .sc design file.")
        if not path.is_file():
            raise ToolError(f"{path} does not exist.")
        if protected.is_copy(str(path)):
            raise ToolError(f"{path} is a copy this bridge made, not a base model. Reopen it with sc_open_copy.")
        fingerprint = file_fingerprint(path)
        previous = protected.entries.get(normalise(str(path)))
        protected.add(str(path), dict(fingerprint, protected_at=now()))
        opened = session().open(str(path))
        state.update(path=str(path), role="base", base=str(path))
        snap = snapshot()
        if not snap["path"] or normalise(snap["path"]) != normalise(str(path)):
            state.update(path=None, role=None)
            raise ToolError(f"SolidCraft opened {snap['path']}, not {path}. Nothing was changed.")
        result = {"path": str(path), "protected": True, "fingerprint": fingerprint, "opened": opened,
                  "features": snap["features"], "bodies": snap["bodies"], "parameters": snap["parameters"]}
        if previous and previous.get("sha256") != fingerprint["sha256"]:
            result["changed_since_last_protected"] = (
                f"This base was protected before ({previous.get('protected_at')}) with another fingerprint: it was changed "
                "outside the bridge since. The new fingerprint is the one checked from now on."
            )
        failed, _ = failed_features(snap)
        if failed:
            result["note"] = "The base has features that fail to rebuild. Tell the engineer before making variants of it."
        return result

    @tool("sc_check_base_untouched", "Read a protected base file's SHA-256 again and compare it with the one recorded. Read-only.", {"path": PATH})
    def check_untouched(args: Dict[str, Any]) -> Dict[str, Any]:
        target = args.get("path") or state["base"]
        if not target:
            raise ToolError("Name the base's path; no base is open in this session.")
        record = protected.entries.get(normalise(target))
        if record is None:
            raise ToolError(f"{target} is not a protected base.")
        if not Path(target).exists():
            return {"path": target, "untouched": False, "detail": "The base file is gone."}
        current = file_fingerprint(Path(target))
        return {"path": target, "untouched": current["sha256"] == record["sha256"], "recorded": record, "current": current}

    # -- copies -----------------------------------------------------------------------------

    def save_new(target: Path, record: Dict[str, Any]) -> Dict[str, Any]:
        """Save the open design to a path that does not exist yet, then register it as the bridge's."""
        reply = session().save(str(target))
        if not reply.get("path") or normalise(reply["path"]) != normalise(str(target)) or not target.exists():
            raise ToolError(f"SolidCraft did not save to {target} (it reports {reply.get('path')}). Nothing was registered.")
        fingerprint = file_fingerprint(target)
        protected.register_copy(str(target), dict(record, sha256=fingerprint["sha256"], saved_at=now()))
        return fingerprint

    @tool(
        "sc_save_copy_and_open",
        "Save the open base as a new .sc file and make that copy the design the bridge works on, before any "
        "change. Refuses unless the open design is a base opened with sc_open_base with no changes. Never "
        "overwrites a file. The base's fingerprint is checked again after the save.",
        {"new_path": PATH}, ["new_path"], read_only=False,
    )
    def save_copy_and_open(args: Dict[str, Any]) -> Dict[str, Any]:
        if state["role"] != "base" or not state["path"]:
            raise ToolError(
                "Refused: the open design is not a base opened with sc_open_base. Every copy starts from the base; a copy "
                "of a copy is refused. Nothing was changed."
            )
        base = state["path"]
        target = check_new_path(args["new_path"], protected)
        snap = snapshot()
        if snap["dirty"]:
            raise ToolError("Refused: the base has unsaved changes in the session, which a copy would carry. Reopen it with sc_open_base. Nothing was changed.")
        record = protected.entries.get(normalise(base)) or {}
        if file_fingerprint(Path(base))["sha256"] != record.get("sha256"):
            raise ToolError(f"Refused: the base {base} changed on disk since it was protected. Tell the engineer. Nothing was changed.")
        fingerprint = save_new(target, {"kind": "copy", "base": base})
        state.update(path=str(target), role="copy", base=base)
        untouched = file_fingerprint(Path(base))["sha256"] == record.get("sha256")
        result = {"path": str(target), "base": base, "fingerprint": fingerprint, "base_untouched": untouched}
        if not untouched:
            result["warning"] = "The base's fingerprint changed during the save. Stop and tell the engineer."
        return result

    @tool(
        "sc_open_copy",
        "Reopen a copy, new part or imported part this bridge made, after SolidCraft or the bridge restarted. "
        "Refuses a retired copy, and one whose file no longer matches the fingerprint of the bridge's last save "
        "(that copy is retired: discard it and make a new one).",
        {"path": PATH}, ["path"], read_only=False,
    )
    def open_copy(args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["path"]
        protected.check_writable(path)
        record = protected.copy_record(path) or {}
        if not Path(path).is_file() or file_fingerprint(Path(path))["sha256"] != record.get("sha256"):
            retire(path, "the file does not match the bridge's last save")
            raise ToolError(f"Refused: {path} is not as the bridge last saved it, so it is retired. Discard it and make a new one. Nothing was changed.")
        session().open(path)
        state.update(path=path, role=record.get("kind", "copy"), base=record.get("base"))
        snap = snapshot()
        if not snap["path"] or normalise(snap["path"]) != normalise(path):
            state.update(path=None, role=None, base=None)
            raise ToolError(f"SolidCraft opened {snap['path']}, not {path}.")
        return {"path": path, "record": record, "features": snap["features"], "bodies": snap["bodies"]}

    @tool(
        "sc_apply_parameters",
        "Apply one parameter set to the copy named by expected_path: checks every name exists and is set directly "
        "(not computed from other parameters), tries the whole set without keeping it, then applies it in one "
        "rebuild and reads every value and feature back. A set that would fail a feature is not applied, and is "
        "reported as failed. Values are expressions as SolidCraft takes them ('42 mm', '30 deg', 'w / 2') or numbers "
        "in mm. Save with sc_save_active afterwards.",
        {"expected_path": PATH, "values": {"type": "object", "minProperties": 1, "maxProperties": 200,
                                           "additionalProperties": {"type": ["number", "string"]}}},
        ["expected_path", "values"], read_only=False,
    )
    def apply_parameters(args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["expected_path"]
        before = require_current(path)
        values = args["values"]
        for name, value in values.items():
            if isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
                raise ToolError(f"{name}: {value!r} is not a number or an expression. Nothing was changed.")
            if isinstance(value, str) and not value.strip():
                raise ToolError(f"{name}: the expression is empty. Nothing was changed.")
        rows = {r["name"]: r for r in session().parameters()}
        problems = []
        for name in values:
            row = rows.get(name)
            if row is None:
                problems.append(f"{name}: no such parameter (SolidCraft would create it)")
            elif row.get("uses"):
                problems.append(f"{name}: its value comes from {', '.join(row['uses'])}; vary that instead")
            elif row.get("unit") == "text":
                problems.append(f"{name}: a text parameter")
            elif row.get("error"):
                problems.append(f"{name}: does not evaluate now ({row['error']})")
        if problems:
            raise ToolError("Refused, nothing was changed: " + "; ".join(problems) + ".")
        trial, trial_error = session().check_parameters(values)
        if trial_error or not isinstance(trial, dict):
            if isinstance(trial, dict) and trial.get("restored") is False:
                retire(path, "trying the parameter set left it in the design")
                raise ToolError(f"Trying the set could not be undone ({trial.get('restore_error')}). The copy {path} is retired: discard it.")
            raise ToolError(f"SolidCraft refused the set: {trial}. Nothing was changed.")
        if trial.get("errors") or trial.get("parameter_errors"):
            return {
                "path": path, "applied": False, "regenerated": False,
                "failed_features": trial.get("errors"), "parameter_errors": trial.get("parameter_errors"),
                "values_tried": trial.get("values"),
                "note": "This set fails to rebuild, so it was not applied: record the variant as regeneration_failed. The copy is unchanged.",
            }
        checkpoint = session().checkpoint()
        try:
            session().change_parameters(values)
        except Refused as err:
            undo = back_to(checkpoint, before, path, "SolidCraft refused the change")
            raise ToolError(f"{err}. " + ("The copy is as it was." if undo["restored"] else f"The copy {path} could not be put back and is retired.")) from err
        after = snapshot()
        failed, warned = failed_features(after)
        read_back = {r["name"]: r.get("value") for r in session().parameters() if r["name"] in values}
        expected = trial.get("values") or {}
        mismatched = [n for n in values if n in expected and (read_back.get(n) is None or abs(read_back[n] - expected[n]) > 1e-9)]
        if failed or mismatched:
            why = "a feature failed after the change" if failed else f"values read back differently ({', '.join(mismatched)})"
            undo = back_to(checkpoint, before, path, why)
            return {"path": path, "applied": False, "regenerated": False, "failed_features": failed,
                    "mismatched": mismatched, "undo": undo,
                    "note": ("The set was undone; the copy is as it was. Record the variant as regeneration_failed."
                             if undo["restored"] else f"The set could not be undone. The copy {path} is retired: discard it.")}
        return {"path": path, "applied": True, "regenerated": True, "values": read_back,
                "warning_features": warned, "unsaved_changes": True,
                "note": "Applied in the session only. Save the variant with sc_save_active."}

    @tool("sc_save_active", "Save the copy named by expected_path, the design open in the bridge's session, to its own file. Refuses any other file.", {"expected_path": PATH}, ["expected_path"], read_only=False)
    def save_active(args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["expected_path"]
        require_current(path)
        try:
            reply = session().save(path)
        except OutcomeUnknown:
            retire(path, "SolidCraft stopped answering during a save")
            raise
        if not reply.get("path") or normalise(reply["path"]) != normalise(path):
            retire(path, f"a save went to {reply.get('path')}")
            raise ToolError(f"SolidCraft saved to {reply.get('path')}, not {path}. The copy is retired.")
        fingerprint = file_fingerprint(Path(path))
        protected.update_copy(path, sha256=fingerprint["sha256"], saved_at=now())
        result = {"path": path, "fingerprint": fingerprint,
                  "backup_note": f"SolidCraft keeps the previous save beside it as {Path(path).name}.bak."}
        if state["base"]:
            result["base_untouched"] = check_untouched({"path": state["base"]})["untouched"]
        return result

    # -- building ---------------------------------------------------------------------------

    @tool(
        "sc_new_part",
        "Start an empty design for an estimating model and save it at new_path at once, so it is a file the bridge "
        "made. Checks that it holds no feature and no body.",
        {"new_path": PATH}, ["new_path"], read_only=False,
    )
    def new_part(args: Dict[str, Any]) -> Dict[str, Any]:
        target = check_new_path(args["new_path"], protected)
        session().new_design(target.stem)
        state.update(path=None, role=None, base=None)
        snap = snapshot()
        if snap["features"] or snap["bodies"]:
            raise ToolError("The new design is not empty. Nothing was saved.")
        fingerprint = save_new(target, {"kind": "new_part"})
        state.update(path=str(target), role="new_part", base=None)
        return {"path": str(target), "fingerprint": fingerprint, "features": [], "bodies": []}

    @tool(
        "sc_import_step",
        "Import a customer's STEP file into a new design and save it at new_path. The STEP file is read, never "
        "changed, and its SHA-256 recorded and checked after. The imported bodies carry no feature history.",
        {"step_path": PATH, "new_path": PATH}, ["step_path", "new_path"], read_only=False,
    )
    def import_step(args: Dict[str, Any]) -> Dict[str, Any]:
        step = Path(args["step_path"])
        if not step.is_absolute() or step.suffix.lower() not in STEP_SUFFIXES or not step.is_file():
            raise ToolError("step_path must be the absolute path of an existing .step or .stp file.")
        if step.stat().st_size > MAX_IMPORT_BYTES:
            raise ToolError(f"{step} is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB; refused.")
        target = check_new_path(args["new_path"], protected)
        step_before = file_fingerprint(step)
        opened = session().open(str(step))
        state.update(path=None, role=None, base=None)
        snap = snapshot()
        if not snap["bodies"]:
            raise ToolError(f"The STEP file opened with no bodies ({opened}). Nothing was saved.")
        fingerprint = save_new(target, {"kind": "import", "step": str(step), "step_sha256": step_before["sha256"]})
        state.update(path=str(target), role="import", base=None)
        return {
            "path": str(target), "fingerprint": fingerprint, "bodies": snap["bodies"], "features": snap["features"],
            "step_untouched": file_fingerprint(step)["sha256"] == step_before["sha256"],
            "step": {k: opened.get(k) for k in ("system", "exporter", "exporter_up", "warnings", "content_sha256")},
        }

    @tool(
        "sc_add_feature",
        "Add one feature to the copy or new part named by expected_path, checked before and after, and undone if a "
        "check fails. kind: sketch (spec: plane Front|Top|Right, entities [{type: rectangle, x1, y1, x2, y2} | "
        "{type: circle, cx, cy, radius} | {type: line, x1, y1, x2, y2}], name?), extrude (spec: sketch_name, "
        "operation NEW|ADD|REMOVE, end BLIND|THROUGH_ALL, depth_mm, opposite_direction?, name?), fillet (spec: "
        "edge_points_mm [[x, y, z], ...], radius_mm, name?) or chamfer (spec: edge_points_mm, distance_mm, name?). "
        "Millimetres throughout. Planes: Front: " + PLANE_AXES["Front"] + "; Top: " + PLANE_AXES["Top"] + "; Right: "
        + PLANE_AXES["Right"] + ". An edge point lies anywhere on a straight edge, or at the midpoint sc_read_faces "
        "gives for a curved one; a point on two edges (a corner) is refused.",
        {"expected_path": PATH, "kind": {"type": "string", "enum": ["sketch", "extrude", "fillet", "chamfer"]},
         "spec": {"type": "object"}},
        ["expected_path", "kind", "spec"], read_only=False,
    )
    def add_feature(args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["expected_path"]
        kind = args["kind"]
        spec = feature_spec(kind, args.get("spec"))
        before = require_current(path)
        existing = [f["name"] for f in before["features"] or []]
        name = spec["name"] or f"{kind.capitalize()} {len(existing) + 1}"
        if name in existing:
            raise ToolError(f"A feature named {name!r} already exists. Give another name. Nothing was changed.")
        failed_before, _ = failed_features(before)
        if failed_before:
            raise ToolError(f"Refused: the design already has failing features ({failed_before}). Nothing was changed.")
        if kind == "extrude" and spec["sketch_name"] not in before["sketches"]:
            raise ToolError(f"There is no sketch named {spec['sketch_name']!r}. Nothing was changed.")
        volume_before = None
        faces_before: Dict[str, int] = {}
        if kind in ("fillet", "chamfer"):
            if not before["bodies"]:
                raise ToolError("There is no body to round or bevel. Nothing was changed.")
            all_edges = []
            for body in before["bodies"]:
                topo = session().topology(body)
                faces_before[body] = len(topo.get("faces") or [])
                all_edges += [dict(e, body=body) for e in topo.get("edges") or []]
            for point in spec["edge_points_mm"]:
                on = edges_at(point, all_edges)
                if len(on) != 1:
                    raise ToolError(
                        f"Edge point {point} lies on {len(on)} edges (it must lie on exactly one; a corner touches several). "
                        "Pick it mid-edge. Nothing was changed."
                    )
        if kind == "extrude" and spec["operation"] != "NEW" and before["bodies"]:
            volume_before = (session().measure().get("total") or {}).get("volume_mm3")

        checkpoint = session().checkpoint()
        made = [name]
        try:
            if kind == "sketch":
                result = session().batch(sketch_commands(spec, name))
                if not result.get("ok", result.get("failed") is None):
                    if not result.get("rolled_back"):
                        back_to(checkpoint, before, path, "a sketch step failed and was not rolled back")
                    raise ToolError(f"The sketch was not made: {result.get('failed')}. Nothing was changed.")
                detail = {"profiles": (result.get("results") or [{}])[-1].get("profiles")}
            elif kind == "extrude":
                detail = session().execute("solid.extrude", extrude_params(spec, name))
            else:
                params = {"edges": spec["edge_points_mm"], "tangent_chain": False, "name": name,
                          ("radius" if kind == "fillet" else "distance"): spec["size_mm"]}
                detail = session().execute("solid." + kind, params)
        except Refused as err:
            after = snapshot()
            if [f["name"] for f in after["features"] or []] != existing:
                back_to(checkpoint, before, path, "a refused step changed the timeline")
            raise ToolError(f"{err}. Nothing was changed.") from err

        problems = []
        try:
            after = snapshot()
            failed, _ = failed_features(after)
            if failed:
                problems.append(f"features fail after the step: {failed}")
            if kind == "sketch" and not detail.get("profiles") and all(e["type"] != "line" for e in spec["entities"]):
                problems.append("the sketch has no closed profile")
            if kind == "extrude":
                if spec["operation"] == "NEW" and not detail.get("created_bodies"):
                    problems.append("no new body was made")
                if spec["operation"] != "NEW":
                    if not detail.get("changed_bodies"):
                        problems.append("no body was changed")
                    volume_after = (session().measure().get("total") or {}).get("volume_mm3")
                    if volume_before is not None and volume_after is not None:
                        if spec["operation"] == "REMOVE" and not volume_after < volume_before:
                            problems.append("the cut removed no material")
                        if spec["operation"] == "ADD" and not volume_after > volume_before:
                            problems.append("the extrude added no material")
            if kind in ("fillet", "chamfer"):
                changed = detail.get("changed_bodies") or []
                if not changed:
                    problems.append("no body was changed")
                for body in changed:
                    added = len(session().topology(body).get("faces") or []) - faces_before.get(body, 0)
                    if added < 1:
                        problems.append(f"{body} gained no face")
            integrity = integrity_compare(before, after, made)
            if integrity["result"] != "clean":
                problems.append("the timeline is not the one before plus this feature")
        except Exception as err:  # noqa: BLE001 - any failure while checking undoes the step
            if isinstance(err, OutcomeUnknown):
                raise
            problems.append(f"the checks after the step stopped with an error ({err})")
            integrity = {"result": "not_checked", "detail": str(err)}
        if problems:
            undo = back_to(checkpoint, before, path, "; ".join(problems))
            raise ToolError(
                "The step was undone because " + "; ".join(problems) + ". "
                + ("The design is as it was." if undo["restored"] else f"It could not be put back: the file {path} is retired.")
            )
        return {"path": path, "kind": kind, "name": name, "detail": detail, "document_integrity": integrity,
                "unsaved_changes": True, "note": "Save with sc_save_active when the build step is done."}

    return server


def main() -> None:
    default_state = Path.home() / ".manufacturing-solidcraft" / "protected.json"
    state_path = Path(os.environ.get("SOLIDCRAFT_BRIDGE_STATE", str(default_state)))
    build_server(start_session, ProtectedFiles(state_path)).serve()


if __name__ == "__main__":
    main()
