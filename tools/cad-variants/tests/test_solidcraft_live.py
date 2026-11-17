"""Live tests of the SolidCraft bridge, against a real SolidCraft engine, on parts made here.

    SOLIDCRAFT_CLI=/path/to/solidcraft-cli python3 -I -m unittest tools/cad-variants/tests/test_solidcraft_live.py -v

Skipped unless SOLIDCRAFT_CLI is set or solidcraft-cli is on the PATH, so CI skips them.
Every file is made in a new temporary folder and deleted after: no one's own model is
opened. What they show: that SolidCraft answers the way the bridge expects, for the
versions they were run with (0.44.0 so far). What they do not show: the desktop app,
Windows or macOS, or a plant's own models.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "common"))
sys.path.insert(0, str(ROOT / "overlays" / "solidcraft" / "servers"))

import solidcraft_bridge as scb  # noqa: E402

CLI = os.environ.get("SOLIDCRAFT_CLI") or shutil.which("solidcraft-cli")


def call(server, name, arguments=None):
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    )
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


@unittest.skipUnless(CLI, "SolidCraft is not installed here (set SOLIDCRAFT_CLI to run these)")
class SolidCraftLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc-live-"))
        self.env = {k: os.environ.get(k) for k in ("SOLIDCRAFT_CLI", "SOLIDCRAFT_ALLOW_PATHS")}
        os.environ["SOLIDCRAFT_CLI"] = CLI
        os.environ["SOLIDCRAFT_ALLOW_PATHS"] = str(self.tmp)
        self.state = self.tmp / "state" / "protected.json"
        self.servers = []
        self.base = self.tmp / "base.sc"
        self.make_base(self.base)

    def tearDown(self):
        for server, holder in self.servers:
            if holder:
                for session in holder:
                    session.close()
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_base(self, path):
        """A 40 x 30 x 20 mm block made outside the bridge: w drives the sketch, h drives the
        extrude's distance through the feature input d1."""
        session = scb.start_session()
        try:
            session._ok("set_parameter", {"name": "w", "value": "40 mm"})
            session._ok("set_parameter", {"name": "h", "value": "20 mm"})
            result = session.batch([
                {"command": "sketch.create", "params": {"plane": "XY", "name": "Base sketch"}},
                {"command": "sketch.rectangle.two_point", "params": {"p0": [0, 0], "p1": ["w", 30]}},
                {"command": "sketch.finish", "params": {}},
                {"command": "solid.extrude", "params": {"distance": "h", "name": "Base"}},
            ])
            self.assertTrue(result.get("ok"), result)
            session.save(str(path))
        finally:
            session.close()

    def server(self):
        sessions = []

        def factory():
            session = scb.start_session()
            sessions.append(session)
            return session

        server = scb.build_server(factory, scb.ProtectedFiles(self.state))
        self.servers.append((server, sessions))
        return server

    def ok(self, server, name, arguments=None):
        value, is_error = call(server, name, arguments)
        self.assertFalse(is_error, f"{name}: {value}")
        return value

    def refused(self, server, name, arguments=None):
        value, is_error = call(server, name, arguments)
        self.assertTrue(is_error, f"{name} should have been refused: {value}")
        return value

    def volume(self, server):
        return self.ok(server, "sc_read_properties")["properties"]["volume_mm3"]

    # -- variants ----------------------------------------------------------------------------

    def test_variant_on_a_copy_leaves_the_base_untouched(self):
        server = self.server()
        status = self.ok(server, "sc_status")
        self.assertEqual(status["solidcraft"]["mode"], "headless")
        base_hash = scb.file_fingerprint(self.base)["sha256"]

        opened = self.ok(server, "sc_open_base", {"path": str(self.base)})
        self.assertTrue(opened["protected"])
        self.assertAlmostEqual(self.volume(server), 40 * 30 * 20, places=3)

        # Nothing writes to the base, even while it is the open design.
        self.refused(server, "sc_apply_parameters", {"expected_path": str(self.base), "values": {"w": "50 mm"}})
        self.refused(server, "sc_save_active", {"expected_path": str(self.base)})

        copy = self.tmp / "V001.sc"
        made = self.ok(server, "sc_save_copy_and_open", {"new_path": str(copy)})
        self.assertTrue(made["base_untouched"])
        self.refused(server, "sc_save_copy_and_open", {"new_path": str(self.tmp / "V002.sc")})  # a copy of a copy

        parameters = {p["name"]: p for p in self.ok(server, "sc_read_parameters")["parameters"]}
        self.assertTrue(parameters["w"]["settable"] and parameters["w"]["drives_geometry"])
        self.assertTrue(parameters["h"]["drives_geometry"])
        self.assertFalse(parameters["d1"]["settable"])

        applied = self.ok(server, "sc_apply_parameters", {"expected_path": str(copy), "values": {"w": "50 mm", "h": 25}})
        self.assertTrue(applied["applied"], applied)
        self.assertAlmostEqual(self.volume(server), 50 * 30 * 25, places=3)
        saved = self.ok(server, "sc_save_active", {"expected_path": str(copy)})
        self.assertTrue(saved["base_untouched"])

        # Refused before anything is tried: a computed parameter, an unknown name.
        self.refused(server, "sc_apply_parameters", {"expected_path": str(copy), "values": {"d1": "30 mm"}})
        self.refused(server, "sc_apply_parameters", {"expected_path": str(copy), "values": {"nope": "1 mm"}})
        names = [p["name"] for p in self.ok(server, "sc_read_parameters")["parameters"]]
        self.assertNotIn("nope", names)

        # A set that fails a feature is not applied, and the copy is as it was.
        failed = self.ok(server, "sc_apply_parameters", {"expected_path": str(copy), "values": {"w": "0 mm"}})
        self.assertFalse(failed["applied"], failed)
        after = {p["name"]: p["value"] for p in self.ok(server, "sc_read_parameters")["parameters"]}
        self.assertAlmostEqual(after["w"], 50.0)
        health = self.ok(server, "sc_read_properties")
        self.assertTrue(health["health"]["regenerated"], health)

        contents = self.ok(server, "sc_list_contents")
        self.assertEqual([f["name"] for f in contents["features"]], ["Base sketch", "Base"])
        self.assertIs(contents["external_references_present"], False)

        self.assertEqual(scb.file_fingerprint(self.base)["sha256"], base_hash)
        self.assertTrue(self.ok(server, "sc_check_base_untouched", {"path": str(self.base)})["untouched"])
        self.assertFalse(Path(str(self.base) + ".bak").exists())

    def test_a_restarted_bridge_reopens_its_copy_and_refuses_a_changed_one(self):
        server = self.server()
        self.ok(server, "sc_open_base", {"path": str(self.base)})
        copy = self.tmp / "V001.sc"
        self.ok(server, "sc_save_copy_and_open", {"new_path": str(copy)})
        self.ok(server, "sc_apply_parameters", {"expected_path": str(copy), "values": {"w": "45 mm"}})
        self.ok(server, "sc_save_active", {"expected_path": str(copy)})

        restarted = self.server()
        reopened = self.ok(restarted, "sc_open_copy", {"path": str(copy)})
        self.assertEqual(reopened["path"], str(copy))
        self.assertAlmostEqual(self.volume(restarted), 45 * 30 * 20, places=3)
        self.refused(restarted, "sc_open_copy", {"path": str(self.base)})  # a base is never a copy

        with copy.open("ab") as handle:  # changed outside the bridge
            handle.write(b"\n")
        again = self.server()
        self.refused(again, "sc_open_copy", {"path": str(copy)})
        self.refused(again, "sc_open_copy", {"path": str(copy)})  # retired for good

    # -- building ----------------------------------------------------------------------------

    def test_build_a_part_from_scratch(self):
        server = self.server()
        part = self.tmp / "EST-1.sc"
        self.ok(server, "sc_new_part", {"new_path": str(part)})
        self.refused(server, "sc_new_part", {"new_path": str(part)})  # never overwrites
        add = lambda kind, spec: self.ok(server, "sc_add_feature", {"expected_path": str(part), "kind": kind, "spec": spec})  # noqa: E731

        add("sketch", {"plane": "Top", "entities": [{"type": "rectangle", "x1": 0, "y1": 0, "x2": 60, "y2": 40}], "name": "Plate sketch"})
        add("extrude", {"sketch_name": "Plate sketch", "operation": "NEW", "depth_mm": 10, "name": "Plate"})
        self.assertAlmostEqual(self.volume(server), 60 * 40 * 10, places=3)
        add("sketch", {"plane": "Top", "entities": [{"type": "circle", "cx": 30, "cy": 20, "radius": 5}], "name": "Hole sketch"})
        add("extrude", {"sketch_name": "Hole sketch", "operation": "REMOVE", "end": "THROUGH_ALL", "name": "Hole"})
        hole_volume = 60 * 40 * 10 - 3.141592653589793 * 25 * 10
        self.assertAlmostEqual(self.volume(server), hole_volume, delta=1.0)

        faces = self.ok(server, "sc_read_faces")["bodies"][0]["faces"]
        cylinders = [f for f in faces if f["surface"] == "cylinder"]
        self.assertEqual(len(cylinders), 1)
        self.assertAlmostEqual(cylinders[0]["cylinder"]["radius"], 5.0)

        # A corner touches two edges; a radius too big does not fit. Both refused, nothing changed.
        self.refused(server, "sc_add_feature", {"expected_path": str(part), "kind": "fillet", "spec": {"edge_points_mm": [[0, 0, 10]], "radius_mm": 2}})
        self.refused(server, "sc_add_feature", {"expected_path": str(part), "kind": "fillet", "spec": {"edge_points_mm": [[30, 0, 10]], "radius_mm": 50}})
        self.refused(server, "sc_add_feature", {"expected_path": str(part), "kind": "extrude", "spec": {"sketch_name": "No such sketch", "depth_mm": 5}})
        self.assertEqual(len(self.ok(server, "sc_list_contents")["features"]), 4)

        filleted = add("fillet", {"edge_points_mm": [[30, 0, 10]], "radius_mm": 2})
        self.assertEqual(filleted["document_integrity"]["result"], "clean")
        add("chamfer", {"edge_points_mm": [[30, 40, 10]], "distance_mm": 1})
        self.assertLess(self.volume(server), hole_volume)

        # The Front plane extrudes toward -Y, as documented.
        add("sketch", {"plane": "Front", "entities": [{"type": "rectangle", "x1": 0, "y1": 10, "x2": 10, "y2": 20}], "name": "Boss sketch"})
        add("extrude", {"sketch_name": "Boss sketch", "operation": "NEW", "depth_mm": 5, "name": "Boss"})
        bodies = self.ok(server, "sc_read_properties")["properties"]["bodies"]
        boss = [b for b in bodies if b["name"] != "Body1"]
        self.assertEqual(len(boss), 1, bodies)
        self.assertLessEqual(boss[0]["bbox"]["max"][1], 1e-6, boss)

        contents = self.ok(server, "sc_list_contents")
        self.assertEqual([f["name"] for f in contents["features"]],
                         ["Plate sketch", "Plate", "Hole sketch", "Hole", "Fillet 5", "Chamfer 6", "Boss sketch", "Boss"])
        self.ok(server, "sc_save_active", {"expected_path": str(part)})
        self.assertTrue(part.exists())

    def test_import_step(self):
        session = scb.start_session()
        step = self.tmp / "customer" / "plate.step"
        step.parent.mkdir()
        try:
            session.new_design("plate")
            session.batch(scb.sketch_commands(scb.feature_spec("sketch", {"plane": "Top", "entities": [{"type": "rectangle", "x1": 0, "y1": 0, "x2": 20, "y2": 10}]}), "S"))
            session.execute("solid.extrude", {"sketch": "S", "distance": 3, "name": "E"})
            session._ok("export", {"path": str(step)})
        finally:
            session.close()
        step_hash = scb.file_fingerprint(step)["sha256"]

        server = self.server()
        imported = self.ok(server, "sc_import_step", {"step_path": str(step), "new_path": str(self.tmp / "EST-2.sc")})
        self.assertTrue(imported["step_untouched"])
        self.assertTrue(imported["bodies"])
        self.assertAlmostEqual(self.volume(server), 20 * 10 * 3, delta=0.01)
        self.assertEqual(scb.file_fingerprint(step)["sha256"], step_hash)
        self.refused(server, "sc_import_step", {"step_path": str(step), "new_path": str(self.tmp / "EST-2.sc")})


if __name__ == "__main__":
    unittest.main()
