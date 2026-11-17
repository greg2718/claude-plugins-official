"""Tests for the SolidCraft bridge, without SolidCraft.

    python3 -I -m unittest discover -s tools/cad-variants/tests -v

A fake session stands in for SolidCraft's MCP server: designs are small JSON files on
disk, so fingerprints and restarts behave as they would. What these cover: every write
guard (writes only to a bridge-made file, protected base, wrong file, computed or unknown
parameter, existing path, unreadable state), a parameter set that would fail, an undo that
does not read back, a build step that fails its checks, SolidCraft stopping mid-call, and
the pure helpers. Whether SolidCraft answers as the fake does is what
test_solidcraft_live.py checks, where SolidCraft is installed.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "common"))
sys.path.insert(0, str(ROOT / "overlays" / "solidcraft" / "servers"))

import solidcraft_bridge as scb  # noqa: E402


def call(server, name, arguments=None):
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    )
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def block_design(w=40.0, h=20.0):
    """The design the fake keeps: a w x 30 x h block. w drives the sketch, h the extrude through d1."""
    return {
        "params": [
            {"name": "w", "expression": f"{w} mm", "value": w, "unit": "mm", "source": "user", "uses": [], "used_by": ["Feature:Base sketch"], "error": None},
            {"name": "h", "expression": f"{h} mm", "value": h, "unit": "mm", "source": "user", "uses": [], "used_by": ["d1"], "error": None},
            {"name": "d1", "expression": "h", "value": h, "unit": "mm", "source": "feature", "uses": ["h"], "used_by": [], "error": None, "feature": "Base", "input": "Distance"},
        ],
        "timeline": [{"id": 1, "name": "Base sketch", "type": "Sketch"}, {"id": 2, "name": "Base", "type": "ExtrudeFeature"}],
        "sketches": ["Base sketch"],
        "bodies": ["Body1"],
        "components": [],
        "faces": {"Body1": 6},
    }


class FakeSolidCraft:
    """One headless SolidCraft session, as the bridge's SolidCraftSession sees it."""

    def __init__(self):
        self.path = None
        self.dirty = False
        self.design = None
        self.checkpoints = {}
        self.next_checkpoint = 7  # SolidCraft's ids do not start at 1
        self.closed = False
        # Knobs for failure cases.
        self.fail_on = {}           # method name -> exception to raise
        self.dry_run = None         # replaces check_parameters' answer
        self.change_reads_back = None  # values parameters.change really leaves
        self.restore_works = True
        self.extrude_result = None
        self.fillet_adds_faces = 1
        self.calls = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.fail_on:
            raise self.fail_on.pop(name)

    # -- the SolidCraftSession surface ------------------------------------------------------

    def close(self):
        self.closed = True

    def version(self):
        self._maybe_fail("version")
        return {"version": "0.44.0", "commit": "a40f6d8", "mode": "headless"}

    def inspect(self):
        self._maybe_fail("inspect")
        d = self.design or {"params": [], "timeline": [], "sketches": [], "bodies": []}
        return {
            "path": self.path,
            "dirty": self.dirty,
            "params": [{k: p[k] for k in ("name", "expression", "value", "unit")} for p in d["params"] if p["source"] == "user"],
            "timeline": copy.deepcopy(d["timeline"]),
            "bodies": list(d["bodies"]),
            "sketches": [{"name": s} for s in d["sketches"]],
        }

    def parameters(self):
        self._maybe_fail("parameters")
        return copy.deepcopy(self.design["params"])

    def components(self):
        return {"components": [{"id": 0, "name": "root"}] + [{"id": i + 1, "name": n} for i, n in enumerate(self.design["components"])]}

    def measure(self):
        values = {p["name"]: p["value"] for p in self.design["params"]}
        volume = values.get("w", 0) * 30 * values.get("h", 0) - 10 * self.design.get("cuts", 0) + 10 * self.design.get("adds", 0)
        return {"bodies": [{"name": b, "volume_mm3": volume} for b in self.design["bodies"]], "body_count": len(self.design["bodies"]),
                "total": {"volume_mm3": volume}}

    def topology(self, body):
        edges = [
            {"index": 0, "start": [0, 0, 20], "end": [40, 0, 20], "mid": [20, 0, 20], "length": 40.0},
            {"index": 1, "start": [0, 0, 20], "end": [0, 30, 20], "mid": [0, 15, 20], "length": 30.0},
        ]
        return {"edges": edges, "faces": [{"index": i, "name": f"F{i}"} for i in range(self.design["faces"].get(body, 0))]}

    def cylinders(self, body):
        return []

    def open(self, path):
        self._maybe_fail("open")
        data = json.loads(Path(path).read_text())
        self.design = data if "timeline" in data else block_design()
        self.path = path
        self.dirty = False
        return {"errors": 0, "features": len(self.design["timeline"]), "path": path}

    def save(self, path):
        self._maybe_fail("save")
        Path(path).write_text(json.dumps(self.design))
        self.path = path
        self.dirty = False
        return {"path": path}

    def new_design(self, name):
        self.design = {"params": [], "timeline": [], "sketches": [], "bodies": [], "components": [], "faces": {}}
        self.path = None
        self.dirty = False
        return {"name": name}

    def check_parameters(self, values):
        self._maybe_fail("check_parameters")
        if self.dry_run is not None:
            return self.dry_run, False
        out = {k: float(str(v).split()[0]) for k, v in values.items()}
        errors = [{"error": "no closed profile", "feature": 2, "name": "Base"}] if any(v <= 0 for v in out.values()) else []
        return {"errors": errors, "parameter_errors": [], "values": out, "restored": True}, False

    def change_parameters(self, values):
        self._maybe_fail("change_parameters")
        leaves = self.change_reads_back or {k: float(str(v).split()[0]) for k, v in values.items()}
        for p in self.design["params"]:
            if p["name"] in leaves:
                p["value"] = leaves[p["name"]]
                p["expression"] = str(values.get(p["name"], leaves[p["name"]]))
        for p in self.design["params"]:
            if p["uses"]:
                p["value"] = next(q["value"] for q in self.design["params"] if q["name"] == p["uses"][0])
        self.dirty = True
        return {"changed": list(values)}

    def checkpoint(self):
        self.next_checkpoint += 3
        self.checkpoints[self.next_checkpoint] = (copy.deepcopy(self.design), self.dirty)
        return self.next_checkpoint

    def restore(self, checkpoint):
        self.calls.append("restore")
        if not self.restore_works:
            return False
        self.design, self.dirty = copy.deepcopy(self.checkpoints[checkpoint])
        return True

    def batch(self, commands):
        self._maybe_fail("batch")
        name = commands[0]["params"]["name"]
        self.design["timeline"].append({"id": len(self.design["timeline"]) + 1, "name": name, "type": "Sketch"})
        self.design["sketches"].append(name)
        self.dirty = True
        return {"ok": True, "completed": len(commands), "results": [{}] * (len(commands) - 1) + [{"profiles": 1}]}

    def execute(self, command, params):
        self._maybe_fail(command)
        name = params["name"]
        self.design["timeline"].append({"id": len(self.design["timeline"]) + 1, "name": name, "type": command})
        self.dirty = True
        if command == "solid.extrude":
            if self.extrude_result is not None:
                return self.extrude_result
            if params["operation"] == "new":
                body = f"Body{len(self.design['bodies']) + 1}"
                self.design["bodies"].append(body)
                self.design["faces"][body] = 6
                return {"created_bodies": [body], "changed_bodies": []}
            self.design["cuts" if params["operation"] == "cut" else "adds"] = self.design.get("cuts" if params["operation"] == "cut" else "adds", 0) + 1
            return {"created_bodies": [], "changed_bodies": ["Body1"]}
        self.design["faces"]["Body1"] = self.design["faces"].get("Body1", 6) + self.fillet_adds_faces
        return {"changed_bodies": ["Body1"]}


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc-fake-"))
        self.base = self.tmp / "base.sc"
        self.base.write_text(json.dumps(block_design()))
        self.state = self.tmp / "state" / "protected.json"
        self.fakes = []
        self.server = self.make_server()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_server(self):
        def factory():
            fake = FakeSolidCraft()
            self.fakes.append(fake)
            return fake
        return scb.build_server(factory, scb.ProtectedFiles(self.state))

    @property
    def fake(self):
        return self.fakes[-1]

    def ok(self, name, arguments=None, server=None):
        value, is_error = call(server or self.server, name, arguments)
        self.assertFalse(is_error, f"{name}: {value}")
        return value

    def refused(self, name, arguments=None, server=None):
        value, is_error = call(server or self.server, name, arguments)
        self.assertTrue(is_error, f"{name} should have been refused: {value}")
        return value["error"]

    def copy_ready(self, name="V001.sc"):
        self.ok("sc_open_base", {"path": str(self.base)})
        path = str(self.tmp / name)
        self.ok("sc_save_copy_and_open", {"new_path": path})
        return path


class HelperTests(unittest.TestCase):
    def test_feature_spec_checks_everything_first(self):
        with self.assertRaises(scb.ToolError):
            scb.feature_spec("sketch", {"plane": "Side", "entities": [{"type": "circle", "cx": 0, "cy": 0, "radius": 1}]})
        with self.assertRaises(scb.ToolError):
            scb.feature_spec("sketch", {"plane": "Top", "entities": [{"type": "rectangle", "x1": 0, "y1": 0, "x2": 0, "y2": 5}]})
        with self.assertRaises(scb.ToolError):
            scb.feature_spec("extrude", {"sketch_name": "S", "depth_mm": -1})
        with self.assertRaises(scb.ToolError):
            scb.feature_spec("fillet", {"edge_points_mm": [[0, 0]], "radius_mm": 1})
        with self.assertRaises(scb.ToolError):
            scb.feature_spec("fillet", {"edge_points_mm": [[0, 0, float("nan")]], "radius_mm": 1})
        spec = scb.feature_spec("chamfer", {"edge_points_mm": [[1, 2, 3]], "distance_mm": 0.5})
        self.assertEqual(spec["size_mm"], 0.5)

    def test_edges_at_matches_along_straight_edges_and_curved_ones_at_the_midpoint(self):
        straight = {"start": [0, 0, 0], "end": [10, 0, 0], "mid": [5, 0, 0], "length": 10.0}
        arc = {"start": [0, 0, 0], "end": [10, 0, 0], "mid": [5, 5, 0], "length": 15.7}
        self.assertEqual(scb.edges_at([3, 0, 0], [straight, arc]), [straight])
        self.assertEqual(scb.edges_at([5, 5, 0], [straight, arc]), [arc])
        self.assertEqual(scb.edges_at([0, 0, 0], [straight, arc]), [straight])  # an arc end is not its midpoint
        self.assertEqual(scb.edges_at([3, 0.5, 0], [straight]), [])

    def test_extrude_and_sketch_commands(self):
        through = scb.extrude_params(scb.feature_spec("extrude", {"sketch_name": "S", "operation": "REMOVE", "end": "THROUGH_ALL"}), "H")
        self.assertEqual(through, {"sketch": "S", "operation": "cut", "name": "H", "through_all": True, "direction": "symmetric"})
        blind = scb.extrude_params(scb.feature_spec("extrude", {"sketch_name": "S", "depth_mm": 4, "opposite_direction": True}), "E")
        self.assertEqual(blind["distance"], 4.0)
        self.assertEqual(blind["direction"], "negative")
        commands = scb.sketch_commands(scb.feature_spec("sketch", {"plane": "Front", "entities": [{"type": "circle", "cx": 1, "cy": 2, "radius": 3}]}), "S")
        self.assertEqual(commands[0], {"command": "sketch.create", "params": {"plane": "XZ", "flip": True, "name": "S"}})
        self.assertEqual(commands[-1]["command"], "sketch.finish")

    def test_cli_command_is_headless_and_limited_to_the_allowed_folders(self):
        saved = {k: os.environ.get(k) for k in ("SOLIDCRAFT_CLI", "SOLIDCRAFT_ALLOW_PATHS")}
        try:
            os.environ["SOLIDCRAFT_CLI"] = "/opt/sc/solidcraft-cli"
            os.environ["SOLIDCRAFT_ALLOW_PATHS"] = os.pathsep.join(["/models", "/out"])
            argv = scb.cli_command()
            self.assertEqual(argv[:3], ["/opt/sc/solidcraft-cli", "mcp", "--headless"])
            self.assertIn("--require-allow-path", argv)
            self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--allow-path"], ["/models", "/out"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class WriteGuardTests(BridgeTestCase):
    def test_the_base_is_never_written(self):
        self.ok("sc_open_base", {"path": str(self.base)})
        before = self.base.read_bytes()
        self.refused("sc_apply_parameters", {"expected_path": str(self.base), "values": {"w": 50}})
        self.refused("sc_save_active", {"expected_path": str(self.base)})
        self.refused("sc_add_feature", {"expected_path": str(self.base), "kind": "fillet", "spec": {"edge_points_mm": [[20, 0, 20]], "radius_mm": 1}})
        self.assertEqual(self.base.read_bytes(), before)
        self.assertNotIn("save", self.fake.calls)
        self.refused("sc_open_copy", {"path": str(self.base)})

    def test_a_file_the_bridge_did_not_make_is_refused(self):
        other = self.tmp / "other.sc"
        other.write_text(json.dumps(block_design()))
        self.refused("sc_open_copy", {"path": str(other)})
        self.refused("sc_save_active", {"expected_path": str(other)})

    def test_writes_name_the_open_copy(self):
        first = self.copy_ready("V001.sc")
        self.ok("sc_open_base", {"path": str(self.base)})
        second = str(self.tmp / "V002.sc")
        self.ok("sc_save_copy_and_open", {"new_path": second})
        error = self.refused("sc_apply_parameters", {"expected_path": first, "values": {"w": 50}})
        self.assertIn("not " + first, error)

    def test_a_copy_of_a_copy_and_a_changed_base_are_refused(self):
        self.copy_ready()
        self.refused("sc_save_copy_and_open", {"new_path": str(self.tmp / "V002.sc")})
        self.ok("sc_open_base", {"path": str(self.base)})
        self.fake.dirty = True
        self.refused("sc_save_copy_and_open", {"new_path": str(self.tmp / "V003.sc")})
        self.fake.dirty = False
        self.base.write_text(json.dumps(block_design(w=41)))  # changed on disk after it was protected
        self.refused("sc_save_copy_and_open", {"new_path": str(self.tmp / "V004.sc")})

    def test_new_paths_must_be_new_absolute_sc_files(self):
        self.ok("sc_open_base", {"path": str(self.base)})
        taken = self.tmp / "taken.sc"
        taken.write_text("{}")
        (self.tmp / "orphan.sc.bak").write_text("{}")
        for bad in (str(taken), str(self.tmp / "orphan.sc"), "relative.sc", str(self.tmp / "v.step"), str(self.base),
                    str(self.tmp / "missing" / "v.sc")):
            self.refused("sc_save_copy_and_open", {"new_path": bad})
        self.refused("sc_new_part", {"new_path": str(taken)})

    def test_an_unreadable_state_file_stops_every_write(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("{not json")
        server = self.make_server()
        self.refused("sc_open_base", {"path": str(self.base)}, server=server)
        self.refused("sc_new_part", {"new_path": str(self.tmp / "N.sc")}, server=server)
        self.assertEqual(self.state.read_text(), "{not json")


class ParameterTests(BridgeTestCase):
    def test_applies_a_set_and_saves_it(self):
        path = self.copy_ready()
        result = self.ok("sc_apply_parameters", {"expected_path": path, "values": {"w": "50 mm", "h": 25}})
        self.assertTrue(result["applied"])
        self.assertEqual(result["values"], {"w": 50.0, "h": 25.0})
        saved = self.ok("sc_save_active", {"expected_path": path})
        self.assertTrue(saved["base_untouched"])
        self.assertEqual(self.ok("sc_check_base_untouched", {"path": str(self.base)})["untouched"], True)

    def test_refuses_unknown_computed_and_bad_values_before_trying(self):
        path = self.copy_ready()
        for values in ({"nope": 1}, {"d1": 30}, {"w": True}, {"w": "  "}):
            self.refused("sc_apply_parameters", {"expected_path": path, "values": values})
        self.fake.design["params"].append({"name": "label", "expression": "'A'", "value": None, "unit": "text", "source": "user", "uses": [], "used_by": [], "error": None})
        self.refused("sc_apply_parameters", {"expected_path": path, "values": {"label": "B"}})
        self.assertNotIn("check_parameters", self.fake.calls)
        self.assertNotIn("change_parameters", self.fake.calls)

    def test_a_set_that_fails_to_rebuild_is_not_applied(self):
        path = self.copy_ready()
        result = self.ok("sc_apply_parameters", {"expected_path": path, "values": {"w": 0}})
        self.assertFalse(result["applied"])
        self.assertNotIn("change_parameters", self.fake.calls)
        self.assertEqual({p["name"]: p["value"] for p in self.fake.design["params"]}["w"], 40.0)

    def test_a_dry_run_that_was_not_undone_retires_the_copy(self):
        path = self.copy_ready()
        self.fake.dry_run = {"errors": [], "parameter_errors": [], "values": {"w": 50.0}, "restored": False}
        self.fake.check_parameters = lambda values: (self.fake.dry_run, True)
        self.refused("sc_apply_parameters", {"expected_path": path, "values": {"w": 50}})
        self.assertIn("unknown state", self.refused("sc_save_active", {"expected_path": path}))

    def test_a_value_that_reads_back_differently_is_undone(self):
        path = self.copy_ready()
        self.fake.change_reads_back = {"w": 49.0}
        result = self.ok("sc_apply_parameters", {"expected_path": path, "values": {"w": 50}})
        self.assertFalse(result["applied"])
        self.assertTrue(result["undo"]["restored"])
        self.assertEqual({p["name"]: p["value"] for p in self.fake.design["params"]}["w"], 40.0)
        self.ok("sc_save_active", {"expected_path": path})  # still a good copy

    def test_an_undo_that_does_not_read_back_retires_the_copy(self):
        path = self.copy_ready()
        self.fake.change_reads_back = {"w": 49.0}
        self.fake.restore_works = False
        result = self.ok("sc_apply_parameters", {"expected_path": path, "values": {"w": 50}})
        self.assertFalse(result["undo"]["restored"])
        self.refused("sc_save_active", {"expected_path": path})
        self.refused("sc_open_copy", {"path": path})

    def test_a_failed_feature_is_not_measured(self):
        self.ok("sc_open_base", {"path": str(self.base)})
        self.fake.design["timeline"][1]["error"] = "no closed profile"
        result = self.ok("sc_read_properties")
        self.assertFalse(result["health"]["regenerated"])
        self.assertIsNone(result["properties"])

    def test_parameters_say_which_drive_geometry_and_which_can_be_set(self):
        self.ok("sc_open_base", {"path": str(self.base)})
        rows = {r["name"]: r for r in self.ok("sc_read_parameters")["parameters"]}
        self.assertTrue(rows["w"]["drives_geometry"] and rows["w"]["settable"])
        self.assertTrue(rows["h"]["drives_geometry"])
        self.assertFalse(rows["d1"]["settable"])


class RestartTests(BridgeTestCase):
    def test_a_restarted_bridge_reopens_its_copy_and_refuses_a_changed_one(self):
        path = self.copy_ready()
        self.ok("sc_apply_parameters", {"expected_path": path, "values": {"w": 45}})
        self.ok("sc_save_active", {"expected_path": path})
        restarted = self.make_server()
        self.ok("sc_open_copy", {"path": path}, server=restarted)
        Path(path).write_text(json.dumps(block_design(w=99)))
        again = self.make_server()
        self.refused("sc_open_copy", {"path": path}, server=again)
        self.refused("sc_open_copy", {"path": path}, server=again)
        self.assertTrue(json.loads(self.state.read_text())["copies"][scb.normalise(path)]["retired"])

    def test_solidcraft_stopping_drops_the_session(self):
        path = self.copy_ready()
        first = self.fake
        first.fail_on["parameters"] = scb.OutcomeUnknown("SolidCraft did not answer in time.")
        error = self.refused("sc_apply_parameters", {"expected_path": path, "values": {"w": 45}})
        self.assertIn("sc_open_copy", error)
        self.assertTrue(first.closed)
        self.refused("sc_save_active", {"expected_path": path})  # nothing is open now
        self.ok("sc_status")
        self.assertIsNot(self.fake, first)  # a new session was started
        self.ok("sc_open_copy", {"path": path})  # the file is as last saved

    def test_stopping_during_a_save_retires_the_copy(self):
        path = self.copy_ready()
        self.fake.fail_on["save"] = scb.OutcomeUnknown("SolidCraft stopped.")
        self.refused("sc_save_active", {"expected_path": path})
        self.refused("sc_open_copy", {"path": path})


class BuildTests(BridgeTestCase):
    def new_part(self):
        path = str(self.tmp / "EST.sc")
        self.ok("sc_new_part", {"new_path": path})
        return path

    def add(self, path, kind, spec):
        return call(self.server, "sc_add_feature", {"expected_path": path, "kind": kind, "spec": spec})

    def test_builds_and_checks_each_step(self):
        path = self.new_part()
        value, err = self.add(path, "sketch", {"plane": "Top", "entities": [{"type": "rectangle", "x1": 0, "y1": 0, "x2": 40, "y2": 30}]})
        self.assertFalse(err, value)
        self.assertEqual(value["name"], "Sketch 1")
        value, err = self.add(path, "extrude", {"sketch_name": "Sketch 1", "operation": "NEW", "depth_mm": 20})
        self.assertFalse(err, value)
        self.assertEqual(value["document_integrity"]["result"], "clean")
        value, err = self.add(path, "extrude", {"sketch_name": "Nope", "depth_mm": 2})
        self.assertTrue(err)
        value, err = self.add(path, "sketch", {"plane": "Top", "entities": [{"type": "circle", "cx": 1, "cy": 1, "radius": 1}], "name": "Sketch 1"})
        self.assertTrue(err)  # the name is taken

    def test_a_corner_point_is_refused_before_anything_changes(self):
        path = self.copy_ready()
        value, err = self.add(path, "fillet", {"edge_points_mm": [[0, 0, 20]], "radius_mm": 1})
        self.assertTrue(err)
        self.assertIn("2 edges", value["error"])
        self.assertNotIn("solid.fillet", self.fake.calls)

    def test_a_step_that_fails_its_checks_is_undone(self):
        path = self.copy_ready()
        self.fake.fillet_adds_faces = 0
        value, err = self.add(path, "fillet", {"edge_points_mm": [[20, 0, 20]], "radius_mm": 1})
        self.assertTrue(err)
        self.assertIn("gained no face", value["error"])
        self.assertIn("restore", self.fake.calls)
        self.assertEqual([f["name"] for f in self.fake.design["timeline"]], ["Base sketch", "Base"])
        self.ok("sc_save_active", {"expected_path": path})

    def test_a_cut_that_removes_nothing_is_undone(self):
        path = self.copy_ready()
        self.add(path, "sketch", {"plane": "Top", "entities": [{"type": "circle", "cx": 5, "cy": 5, "radius": 2}], "name": "Hole sketch"})
        self.fake.extrude_result = {"created_bodies": [], "changed_bodies": ["Body1"]}
        value, err = self.add(path, "extrude", {"sketch_name": "Hole sketch", "operation": "REMOVE", "end": "THROUGH_ALL"})
        self.assertTrue(err)
        self.assertIn("removed no material", value["error"])

    def test_an_undo_that_fails_retires_the_file(self):
        path = self.copy_ready()
        self.fake.fillet_adds_faces = 0
        self.fake.restore_works = False
        value, err = self.add(path, "fillet", {"edge_points_mm": [[20, 0, 20]], "radius_mm": 1})
        self.assertTrue(err)
        self.assertIn("retired", value["error"])
        self.refused("sc_save_active", {"expected_path": path})

    def test_external_references(self):
        self.ok("sc_open_base", {"path": str(self.base)})
        self.assertIs(self.ok("sc_list_contents")["external_references_present"], False)
        self.fake.design["components"] = ["Bracket"]
        self.assertIsNone(self.ok("sc_list_contents")["external_references_present"])

    def test_step_import_leaves_the_step_file_alone(self):
        step = self.tmp / "plate.step"
        step.write_text(json.dumps(block_design(w=20, h=3)))
        before = step.read_bytes()
        result = self.ok("sc_import_step", {"step_path": str(step), "new_path": str(self.tmp / "EST-2.sc")})
        self.assertTrue(result["step_untouched"])
        self.assertEqual(step.read_bytes(), before)
        self.refused("sc_import_step", {"step_path": str(self.tmp / "missing.step"), "new_path": str(self.tmp / "EST-3.sc")})


if __name__ == "__main__":
    unittest.main()
