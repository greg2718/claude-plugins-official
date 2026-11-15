"""Failure-recovery tests for both bridges: the four issues from the November review.

    python3 -I -m unittest discover -s tools/cad-variants/tests -v

1. A SOLIDWORKS undo that SOLIDWORKS answers with a status, or that does not
   take, must not be reported as undone.
2. A failed rebuild must still count as failed after the bridge restarts.
3. An Onshape write with no answer (a timeout, a dropped connection, a server
   error, or a bridge that stopped mid-write) must quarantine the copy with a
   journal of the attempted change until it is reconciled.
4. Two bridge processes sharing one state file must not undo each other's
   safety marks.

Each test here fails on the bridges at commit 7a86651. Fakes only: none of
this has run against live SOLIDWORKS or Onshape.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import test_bridges as tb  # the fakes and the call helper; its test classes stay in their own module

osb, swb, call = tb.osb, tb.swb, tb.call

D1, D2 = "D1@Boss-Extrude1", "D2@Boss-Extrude1"


class SolidWorksRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.base = self.dir / "Bracket.SLDPRT"
        self.base.write_bytes(b"solid part bytes")
        self.session = tb.FakeSession(tb.FakeDoc(str(self.base), "Bracket.SLDPRT"))
        self.state = self.dir / "state.json"
        self.server = swb.build_server(lambda: self.session, swb.ProtectedFiles(self.state))
        self.copy = self.dir / "Bracket-V001.SLDPRT"
        value, is_error = call(self.server, "sw_save_copy_and_open", {"new_path": str(self.copy)})
        self.assertFalse(is_error, value)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self):
        return json.loads(self.state.read_text("utf-8"))["copies"][swb.normalise(str(self.copy))]

    def assert_retired_and_base_untouched(self):
        self.assertIn("retired", self.record())
        value, is_error = call(self.server, "sw_save_active", {"expected_path": str(self.copy)})
        self.assertTrue(is_error)
        self.assertIn("Discard it", value["error"])
        self.assertEqual(self.base.read_bytes(), b"solid part bytes")

    # Issue 1 ----------------------------------------------------------------

    def test_restore_answered_with_a_status_is_not_reported_as_undone(self):
        self.session.refuse = {D2: "InvalidValue"}  # the second change fails, so the first is undone
        self.session.refuse_values = {(D1, 0.003): "OverDefining"}  # the undo comes back as a status, not an exception
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(self.copy), "dimensions": {D1: 0.004, D2: 0.03}})
        self.assertTrue(is_error)
        self.assertNotIn("were undone", value["error"])
        self.assertIn("unknown state", value["error"])
        self.assert_retired_and_base_untouched()

    def test_restore_that_reports_success_but_does_not_take_is_caught_by_reading_back(self):
        self.session.refuse = {D2: "InvalidValue"}
        real = self.session.set_dimension

        def lying(doc, name, value):
            if (name, value) == (D1, 0.003):
                return "Successful"  # says so, changes nothing
            return real(doc, name, value)

        self.session.set_dimension = lying
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(self.copy), "dimensions": {D1: 0.004, D2: 0.03}})
        self.assertTrue(is_error)
        self.assertIn("unknown state", value["error"])
        self.assert_retired_and_base_untouched()

    def test_global_variable_restore_is_read_back(self):
        self.session.refuse = {D1: "InvalidValue"}
        real = self.session.put_equation
        self.session.put_equation = lambda doc, i, text: None if text == '"wall" = 3mm' else real(doc, i, text)
        value, is_error = call(self.server, "sw_apply_parameters",
                               {"expected_path": str(self.copy), "global_variables": {"wall": "4mm"}, "dimensions": {D1: 0.004}})
        self.assertTrue(is_error)
        self.assertIn("unknown state", value["error"])
        self.assert_retired_and_base_untouched()

    # Issue 2 ----------------------------------------------------------------

    def test_failed_rebuild_still_counts_as_failed_after_a_restart(self):
        self.session.rebuild_ok = False
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(self.copy), "dimensions": {D1: 0.004}})
        self.assertFalse(is_error, value)
        self.assertFalse(value["health"]["regenerated"])
        # The bridge restarts. Nothing in the tree shows an error, so only the record says the variant failed.
        self.session.rebuild_ok = True
        self.session.properties = lambda doc: {"mass_kg": 0.2, "volume_m3": 1e-4}
        restarted = swb.build_server(lambda: self.session, swb.ProtectedFiles(self.state))
        value, is_error = call(restarted, "sw_read_properties")
        self.assertFalse(is_error, value)
        self.assertFalse(value["health"]["regenerated"])
        self.assertIsNone(value["properties"])
        # Only a fresh rebuild that SOLIDWORKS reports as a success clears it.
        value, is_error = call(restarted, "sw_apply_parameters", {"expected_path": str(self.copy), "dimensions": {D1: 0.005}})
        self.assertTrue(value["health"]["regenerated"], value)
        self.assertNotIn("rebuild_failed", self.record())
        self.assertIsNotNone(call(restarted, "sw_read_properties")[0]["properties"])

    def test_unreadable_health_after_a_change_counts_as_a_failed_rebuild(self):
        def broken(doc):
            raise RuntimeError("COM call failed")

        self.session.feature_errors = broken
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(self.copy), "dimensions": {D1: 0.004}})
        self.assertTrue(is_error)
        self.assertIn("Treat this variant as failed", value["error"])
        self.assertIn("rebuild_failed", self.record())

    def test_build_step_whose_check_crashes_is_deleted_again(self):
        part = str(self.dir / "Block.SLDPRT")
        self.assertFalse(call(self.server, "sw_new_part", {"new_path": part})[1])
        self.session.feature_errors = lambda doc: (_ for _ in ()).throw(RuntimeError("COM call failed"))
        before = [f["name"] for f in self.session.features]
        value, is_error = call(self.server, "sw_add_feature", {"expected_path": part, "kind": "sketch",
                                                                "spec": {"plane": "Top", "entities": [{"type": "circle", "cx": 0, "cy": 0, "radius": 1}]}})
        self.assertTrue(is_error)
        self.assertIn("stopped unexpectedly", value["error"])
        self.assertEqual([f["name"] for f in self.session.features], before)

    # Issue 4 ----------------------------------------------------------------

    def test_a_stale_bridge_cannot_undo_a_retirement(self):
        first, stale = swb.ProtectedFiles(self.state), swb.ProtectedFiles(self.state)
        first.retire_copy(str(self.copy), "a change could not be undone")
        stale.register_copy(str(self.dir / "Bracket-V002.SLDPRT"), {"source": str(self.base)})  # writes its own, older view
        self.assertIn("retired", self.record())
        with self.assertRaises(swb.ToolError):
            stale.check_writable(str(self.copy))
        with self.assertRaises(swb.ToolError):
            swb.ProtectedFiles(self.state).check_writable(str(self.copy))

    def test_a_stale_bridge_cannot_drop_another_bridges_copy_or_protection(self):
        first, stale = swb.ProtectedFiles(self.state), swb.ProtectedFiles(self.state)
        other = self.dir / "Other.SLDPRT"
        other.write_bytes(b"other base")
        first.add(str(other), {"sha256": "x"})
        stale.update_copy(str(self.copy), note="written by the stale bridge")
        fresh = swb.ProtectedFiles(self.state)
        self.assertTrue(fresh.is_protected(str(other)))
        self.assertEqual(self.record()["note"], "written by the stale bridge")


class OnshapeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "protected.json"
        self.features = [dict(f) for f in tb.FEATURES]
        self.fake = tb.FakeOnshape({
            ("GET", "/documents/base"): {"name": "Bracket", "defaultWorkspace": {"id": "wb"}},
            ("GET", "/documents/d/base/w/wb/currentmicroversion"): {"microversion": "m1"},
            ("GET", "/documents/copy"): {"name": "Bracket-V001"},
            ("GET", "/partstudios/d/copy/w/wc/e/ec/features"): {"features": self.features, "featureStates": {}, "serializationVersion": "1.2", "sourceMicroversion": "mc"},
            ("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1"): {},
            ("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v2"): {},
            ("POST", "/documents/base/workspaces/wb/copy"): {"newDocumentId": "copy", "newWorkspaceId": "wc"},
        })
        self.timeout_on = set()
        self.server = self.build()
        self.assertFalse(call(self.server, "onshape_protect_base", {"document_id": "base", "workspace_id": "wb"})[1])
        self.assertFalse(call(self.server, "onshape_copy_workspace", {"document_id": "base", "workspace_id": "wb", "new_name": "Bracket-V001"})[1])

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self, method, url, headers, body):
        path = url.split("/api/v10", 1)[1].split("?", 1)[0]
        if (method, path) in self.timeout_on:
            self.fake(method, url, headers, body)  # Onshape got it, and may have applied it
            raise TimeoutError("The read operation timed out")
        return self.fake(method, url, headers, body)

    def build(self):
        return osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=self.transport), osb.ProtectedDocuments(self.state))

    def set_args(self, assignments=None):
        return {"document_id": "copy", "workspace_id": "wc", "element_id": "ec", "element_kind": "PARTSTUDIO",
                "expected_document_name": "Bracket-V001", "assignments": assignments or {"wall": "4 mm"}}

    def record(self):
        return json.loads(self.state.read_text("utf-8"))["copies"]["copy"]

    def set_wall(self, expression):
        self.features[0] = tb.variable_feature("v1", "wall", expression)

    # Issue 3 ----------------------------------------------------------------

    def test_timeout_quarantines_the_copy_with_a_journal(self):
        self.timeout_on = {("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1")}
        value, is_error = call(self.server, "onshape_set_variables", self.set_args())
        self.assertTrue(is_error)
        self.assertIn("quarantined", value["error"])
        attempt = self.record()["unknown_outcome"]
        self.assertEqual(attempt["planned"], [{"name": "wall", "from": "3 mm", "to": "4 mm"}])
        self.assertNotIn("pending", self.record())
        posts_before = len([c for c in self.fake.calls if c[0] == "POST"])
        # No retry, no undo, no version, no build step until it is reconciled, in this bridge or after a restart.
        self.timeout_on = set()
        for server in (self.server, self.build()):
            value, is_error = call(server, "onshape_set_variables", self.set_args({"wall": "3 mm"}))
            self.assertTrue(is_error)
            self.assertIn("onshape_reconcile_copy", value["error"])
            self.assertTrue(call(server, "onshape_create_version", {"document_id": "copy", "workspace_id": "wc", "name": "v"})[1])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), posts_before)

    def test_reconcile_lifts_the_quarantine_when_the_change_did_not_land(self):
        self.timeout_on = {("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1")}
        call(self.server, "onshape_set_variables", self.set_args())
        self.timeout_on = set()
        value, is_error = call(self.server, "onshape_reconcile_copy", {"document_id": "copy"})
        self.assertFalse(is_error, value)
        self.assertTrue(value["outcome"].startswith("not applied"), value)
        self.assertNotIn("unknown_outcome", self.record())
        self.assertFalse(call(self.server, "onshape_set_variables", self.set_args())[1])

    def test_reconcile_lifts_the_quarantine_when_the_change_landed(self):
        self.timeout_on = {("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1")}
        call(self.server, "onshape_set_variables", self.set_args())
        self.set_wall("4 mm")  # Onshape applied it before the connection dropped
        value, is_error = call(self.build(), "onshape_reconcile_copy", {"document_id": "copy"})
        self.assertFalse(is_error, value)
        self.assertTrue(value["outcome"].startswith("applied"), value)
        self.assertNotIn("unknown_outcome", self.record())

    def test_reconcile_retires_a_copy_left_part_changed(self):
        self.timeout_on = {("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v2")}
        call(self.server, "onshape_set_variables", self.set_args({"wall": "4 mm", "rib": "#wall * 3"}))
        self.set_wall("4 mm")  # the first change landed; the second's outcome is unknown and did not land
        value, is_error = call(self.server, "onshape_reconcile_copy", {"document_id": "copy"})
        self.assertFalse(is_error, value)
        self.assertEqual(value["outcome"], "unknown, copy retired")
        self.assertIn("retired", self.record())
        self.assertIn("Discard it", call(self.server, "onshape_set_variables", self.set_args())[0]["error"])

    def test_a_server_error_on_a_write_is_an_unknown_outcome_too(self):
        self.fake.routes[("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1")] = (502, {"message": "bad gateway"})
        value, is_error = call(self.server, "onshape_set_variables", self.set_args())
        self.assertTrue(is_error)
        self.assertIn("quarantined", value["error"])
        self.assertIn("unknown_outcome", self.record())

    def test_a_write_left_pending_by_a_crash_is_refused_after_restart(self):
        osb.ProtectedDocuments(self.state).update_copy(
            "copy", pending={"kind": "set_variables", "element_kind": "PARTSTUDIO", "workspace_id": "wc", "element_id": "ec",
                             "planned": [{"name": "wall", "from": "3 mm", "to": "4 mm"}], "started_at": "2026-11-15T00:00:00+00:00"})
        restarted = self.build()
        value, is_error = call(restarted, "onshape_set_variables", self.set_args())
        self.assertTrue(is_error)
        self.assertIn("onshape_reconcile_copy", value["error"])
        value, is_error = call(restarted, "onshape_reconcile_copy", {"document_id": "copy"})
        self.assertFalse(is_error, value)
        self.assertNotIn("pending", self.record())

    def test_build_step_with_no_answer_is_reconciled_by_deleting_it(self):
        studio = tb.FakeStudio()
        timeouts = {"on": True}

        def flaky(method, url, headers, body):
            answer = studio(method, url, headers, body)
            if timeouts["on"] and method == "POST" and url.split("?")[0].endswith("/features"):
                raise TimeoutError("The read operation timed out")  # the feature was added; the answer was lost
            return answer

        state = Path(self.tmp.name) / "build.json"
        server = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=flaky, sleep=lambda _: None), osb.ProtectedDocuments(state))
        doc, is_error = call(server, "onshape_create_document", {"name": "Block"})
        self.assertFalse(is_error, doc)
        spec = {"plane": "Top", "entities": [{"type": "circle", "cx": 0, "cy": 0, "radius": 5}]}
        args = {"document_id": doc["document_id"], "workspace_id": doc["workspace_id"], "element_id": doc["element_id"],
                "expected_document_name": "Block", "kind": "sketch", "spec": spec}
        value, is_error = call(server, "onshape_add_feature", args)
        self.assertTrue(is_error)
        self.assertIn("quarantined", value["error"])
        features = studio.docs[doc["document_id"]]["studios"][doc["element_id"]]["features"]
        self.assertEqual(len(features), 1)  # it is there, unchecked
        timeouts["on"] = False
        self.assertIn("onshape_reconcile_copy", call(server, "onshape_add_feature", args)[0]["error"])
        value, is_error = call(server, "onshape_reconcile_copy", {"document_id": doc["document_id"]})
        self.assertFalse(is_error, value)
        self.assertIn("deleted again", value["outcome"])
        self.assertEqual(studio.docs[doc["document_id"]]["studios"][doc["element_id"]]["features"], [])
        self.assertFalse(call(server, "onshape_add_feature", args)[1])

    def test_live_test_fault_drops_one_answer_only(self):
        osb._TEST_DROP_ANSWER["armed"] = True
        try:
            value, is_error = call(self.server, "onshape_set_variables", self.set_args())
            self.assertIn("Test fault", value["error"])
            self.assertIn("unknown_outcome", self.record())
            self.assertFalse(osb._TEST_DROP_ANSWER["armed"])
        finally:
            osb._TEST_DROP_ANSWER["armed"] = False

    def test_new_document_with_no_answer_is_never_written(self):
        self.fake.routes[("POST", "/documents")] = {"id": "ghost", "defaultWorkspace": {"id": "wg"}}
        self.timeout_on = {("POST", "/documents")}
        value, is_error = call(self.server, "onshape_create_document", {"name": "Block"})
        self.assertTrue(is_error)
        self.assertIn("may have made a document", value["error"])
        self.assertNotIn("ghost", json.loads(self.state.read_text("utf-8"))["copies"])

    # Issue 4 ----------------------------------------------------------------

    def test_a_stale_bridge_cannot_undo_a_retirement(self):
        first, stale = osb.ProtectedDocuments(self.state), osb.ProtectedDocuments(self.state)
        first.retire_copy("copy", "a change could not be undone")
        stale.register_copy("copy2", {"workspace_id": "wc2", "name": "Bracket-V002"})  # writes its own, older view
        self.assertIn("retired", self.record())
        for bridge in (stale, osb.ProtectedDocuments(self.state)):
            with self.assertRaises(osb.ToolError):
                bridge.check_writable("copy", "wc")

    def test_a_stale_bridge_cannot_lift_a_quarantine_or_revive_a_reconciled_one(self):
        first, stale = osb.ProtectedDocuments(self.state), osb.ProtectedDocuments(self.state)
        attempt = {"kind": "create_version", "name": "v", "started_at": "2026-11-15T01:00:00+00:00"}
        first.quarantine("copy", attempt)
        stale.update_copy("copy", note="written by the stale bridge")
        self.assertEqual(self.record()["unknown_outcome"], attempt)
        # The stale bridge now remembers the quarantine. Once it is reconciled, that memory must not bring it back.
        first.resolve_unknown("copy", attempt, "not applied")
        stale.update_copy("copy", note="written again by the stale bridge")
        self.assertNotIn("unknown_outcome", self.record())
        stale.check_writable("copy", "wc")


if __name__ == "__main__":
    unittest.main()
