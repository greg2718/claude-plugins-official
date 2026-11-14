"""Tests for the CAD bridges, without SOLIDWORKS or an Onshape account.

    python3 -I -m unittest discover -s tools/cad-variants/tests -v

What these cover: the MCP framing, Onshape request signing against a vector
computed independently, the pure helpers, and every write guard (writes only
to a bridge-made copy, protected base, wrong document, driven dimension,
existing path, unreadable state), failed and unknown regeneration, base
checks with missing evidence, and whole-batch validation and undo, through
fake CAD sessions. What they cannot cover: whether SOLIDWORKS and Onshape answer the
way their documentation says. That needs a live run.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "common"))
sys.path.insert(0, str(ROOT / "overlays" / "onshape" / "servers"))
sys.path.insert(0, str(ROOT / "overlays" / "solidworks" / "servers"))

import mcp_stdio  # noqa: E402
import onshape_bridge as osb  # noqa: E402
import solidworks_bridge as swb  # noqa: E402


def call(server, name, arguments=None):
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    )
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


# ---------------------------------------------------------------------------
# MCP layer
# ---------------------------------------------------------------------------


class McpStdioTests(unittest.TestCase):
    def make(self):
        server = mcp_stdio.Server("t", "0")

        @server.tool("echo", "Echo.", {"x": {"type": "string"}}, ["x"])
        def echo(args):
            return {"x": args["x"]}

        @server.tool("fail", "Fail.")
        def fail(_):
            raise mcp_stdio.ToolError("nope")

        return server

    def test_initialize_negotiates_version(self):
        server = self.make()
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(reply["result"]["protocolVersion"], mcp_stdio.SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_notification_gets_no_reply(self):
        self.assertIsNone(self.make().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tool_call_and_errors(self):
        server = self.make()
        self.assertEqual(call(server, "echo", {"x": "a"}), ({"x": "a"}, False))
        value, is_error = call(server, "echo", {})
        self.assertTrue(is_error)
        self.assertIn("Missing", value["error"])
        value, is_error = call(server, "echo", {"x": "a", "y": 1})
        self.assertTrue(is_error)
        self.assertEqual(call(server, "fail"), ({"error": "nope"}, True))
        reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "missing"}})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_serve_writes_only_frames(self):
        server = self.make()
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
            "not json\n"
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        )
        stdout = io.StringIO()
        server.serve(stdin, stdout)
        frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([f.get("id") for f in frames], [1, None, 2])
        self.assertEqual(frames[1]["error"]["code"], -32700)
        self.assertEqual([t["name"] for t in frames[2]["result"]["tools"]], ["echo", "fail"])


# ---------------------------------------------------------------------------
# Onshape
# ---------------------------------------------------------------------------


class FakeOnshape:
    """A transport that answers from a route table and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.urls = []

    def __call__(self, method, url, headers, body):
        path = url.split("/api/v10", 1)[1].split("?", 1)[0]
        self.urls.append(url)
        self.calls.append((method, path, json.loads(body) if body else None, headers))
        answer = self.routes.get((method, path))
        if answer is None:
            return 404, {}, b"{}"
        status, payload = answer if isinstance(answer, tuple) else (200, answer)
        return status, {}, json.dumps(payload).encode()


def variable_feature(fid, name, expression):
    return {
        "featureType": "assignVariable",
        "featureId": fid,
        "name": f"Variable {name}",
        "parameters": [
            {"parameterId": "name", "value": name},
            {"parameterId": "lengthValue", "expression": expression},
        ],
    }


def extrude(fid, expression):
    return {"featureType": "extrude", "featureId": fid, "name": f"Extrude {fid}", "parameters": [{"parameterId": "depth", "expression": expression}]}


FEATURES = [
    variable_feature("v1", "wall", "3 mm"),
    variable_feature("v2", "rib", "#wall * 2"),
    variable_feature("v3", "unused", "1 mm"),
    extrude("e1", "#rib + 1 mm"),
]


class OnshapeSigningTests(unittest.TestCase):
    def test_signature_matches_independent_computation(self):
        # The string Onshape's API-key page describes: method, nonce, date, content type,
        # path, query, each followed by a newline, lower-cased, HMAC-SHA256, base64.
        method, nonce, date = "GET", "AbCdEfGhIjKlMnOpQrStUvWxY", "Mon, 09 Nov 2026 12:00:00 GMT"
        ctype, path, query = "application/json", "/api/v10/documents/abc", "a=1&B=2"
        expected = base64.b64encode(
            hmac.new(
                b"SeCrEt",
                (f"{method}\n{nonce}\n{date}\n{ctype}\n{path}\n{query}\n").lower().encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        header = osb.sign_request(method, path, query, ctype, "ACCESS", "SeCrEt", nonce, date)
        self.assertEqual(header, f"On ACCESS:HmacSHA256:{expected}")

    def test_client_refuses_missing_keys(self):
        with self.assertRaises(osb.OnshapeError):
            osb.OnshapeClient("", "")

    def test_client_retries_429_once_and_maps_402(self):
        answers = iter([(429, {"Retry-After": "2"}, b""), (200, {}, b'{"ok": true}')])
        waits = []
        client = osb.OnshapeClient("a", "s", transport=lambda *a: next(answers), sleep=waits.append)
        self.assertEqual(client.request("GET", "/x"), {"ok": True})
        self.assertEqual(waits, [2.0])
        client = osb.OnshapeClient("a", "s", transport=lambda *a: (402, {}, b""), sleep=waits.append)
        with self.assertRaisesRegex(osb.OnshapeError, "annual API call limit"):
            client.request("GET", "/x")

    def test_request_headers_are_signed_over_the_sent_path(self):
        fake = FakeOnshape({("GET", "/documents/d1"): {"name": "Base"}})
        client = osb.OnshapeClient("a", "s", transport=fake)
        client.request("GET", "/documents/d1", {"q": "1"})
        headers = fake.calls[0][3]
        again = osb.sign_request("GET", "/api/v10/documents/d1", "q=1", "application/json", "a", "s", headers["On-Nonce"], headers["Date"])
        self.assertEqual(headers["Authorization"], again)


class OnshapeHelperTests(unittest.TestCase):
    def test_variables_and_driving(self):
        names = [v["name"] for v in osb.variable_features(FEATURES)]
        self.assertEqual(names, ["wall", "rib", "unused"])
        refs = osb.references_by_variable(FEATURES, names)
        self.assertEqual(refs, {"wall": ["Variable rib"], "rib": ["Extrude e1"], "unused": []})
        self.assertEqual(osb.driving_variables(FEATURES, names), {"wall": True, "rib": True, "unused": False})

    def test_variable_cycle_does_not_drive(self):
        features = [variable_feature("a", "x", "#y"), variable_feature("b", "y", "#x")]
        self.assertEqual(osb.driving_variables(features, ["x", "y"]), {"x": False, "y": False})

    def test_feature_health(self):
        response = {
            "features": [{"featureId": "f1", "name": "Extrude 1"}],
            "featureStates": {
                "f1": {"featureStatus": "OK"},
                "f2": {"featureStatus": "WARNING"},
                "f3": {"featureStatus": "ERROR"},
                "f4": {"featureStatus": "ERROR", "inactive": True},
            },
        }
        health = osb.feature_health(response)
        self.assertFalse(health["regenerated"])
        self.assertEqual([f["feature_id"] for f in health["failed_features"]], ["f3"])
        self.assertEqual([f["feature_id"] for f in health["warning_features"]], ["f2"])

    def test_mass_absent_without_material(self):
        out = osb.summarize_mass({"bodies": {"-all-": {"hasMass": False, "mass": [0, 0, 0], "volume": [1e-5, 1e-5, 1e-5]}}})
        self.assertIsNone(out["-all-"]["mass_kg"])
        self.assertEqual(out["-all-"]["volume_m3"], 1e-5)


class OnshapeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "protected.json"
        self.fake = FakeOnshape(
            {
                ("GET", "/documents/base"): {"name": "Bracket", "defaultWorkspace": {"id": "wb"}},
                ("GET", "/documents/d/base/w/wb/currentmicroversion"): {"microversion": "m1"},
                ("GET", "/documents/copy"): {"name": "Bracket-V001"},
                ("GET", "/partstudios/d/copy/w/wc/e/ec/features"): {
                    "features": FEATURES,
                    "featureStates": {},
                    "serializationVersion": "1.2",
                    "sourceMicroversion": "mc",
                },
                ("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1"): {},
                ("POST", "/documents/base/workspaces/wb/copy"): {"newDocumentId": "copy", "newWorkspaceId": "wc"},
            }
        )
        self.server = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=self.fake), osb.ProtectedDocuments(self.state))

    def tearDown(self):
        self.tmp.cleanup()

    def writes(self):
        return [c for c in self.fake.calls if c[0] == "POST" and not c[1].endswith("/copy")]

    def test_protect_persists_and_refuses_writes(self):
        value, is_error = call(self.server, "onshape_protect_base", {"document_id": "base", "workspace_id": "wb"})
        self.assertFalse(is_error, value)
        self.assertEqual(value["microversion"], "m1")
        restarted = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=self.fake), osb.ProtectedDocuments(self.state))
        value, is_error = call(
            restarted,
            "onshape_set_variables",
            {"document_id": "base", "workspace_id": "wb", "element_id": "e", "element_kind": "PARTSTUDIO",
             "expected_document_name": "Bracket", "assignments": {"wall": "4 mm"}},
        )
        self.assertTrue(is_error)
        self.assertIn("protected base", value["error"])
        self.assertEqual(self.writes(), [])

    def test_untouched_check(self):
        call(self.server, "onshape_protect_base", {"document_id": "base", "workspace_id": "wb"})
        self.assertTrue(call(self.server, "onshape_check_base_untouched", {"document_id": "base"})[0]["untouched"])
        self.fake.routes[("GET", "/documents/d/base/w/wb/currentmicroversion")] = {"microversion": "m2"}
        self.assertFalse(call(self.server, "onshape_check_base_untouched", {"document_id": "base"})[0]["untouched"])

    def make_copy(self):
        copy, is_error = call(self.server, "onshape_copy_workspace", {"document_id": "base", "workspace_id": "wb", "new_name": "Bracket-V001"})
        self.assertFalse(is_error, copy)
        return copy

    def test_wrong_name_refused(self):
        self.make_copy()
        value, is_error = call(
            self.server,
            "onshape_set_variables",
            {"document_id": "copy", "workspace_id": "wc", "element_id": "ec", "element_kind": "PARTSTUDIO",
             "expected_document_name": "Something else", "assignments": {"wall": "4 mm"}},
        )
        self.assertTrue(is_error)
        self.assertEqual(self.writes(), [])

    def test_unknown_variable_refused_before_any_write(self):
        self.make_copy()
        value, is_error = call(
            self.server,
            "onshape_set_variables",
            {"document_id": "copy", "workspace_id": "wc", "element_id": "ec", "element_kind": "PARTSTUDIO",
             "expected_document_name": "Bracket-V001", "assignments": {"wall": "4 mm", "nope": "1 mm"}},
        )
        self.assertTrue(is_error)
        self.assertIn("nope", value["error"])
        self.assertEqual(self.writes(), [])

    def test_set_variable_on_copy(self):
        call(self.server, "onshape_protect_base", {"document_id": "base", "workspace_id": "wb"})
        copy, is_error = call(self.server, "onshape_copy_workspace", {"document_id": "base", "workspace_id": "wb", "new_name": "Bracket-V001"})
        self.assertFalse(is_error, copy)
        value, is_error = call(
            self.server,
            "onshape_set_variables",
            {"document_id": copy["new_document_id"], "workspace_id": copy["new_workspace_id"], "element_id": "ec",
             "element_kind": "PARTSTUDIO", "expected_document_name": "Bracket-V001", "assignments": {"wall": "4 mm"}},
        )
        self.assertFalse(is_error, value)
        self.assertEqual(value["changes"], [{"name": "wall", "from": "3 mm", "to": "4 mm", "written": True}])
        self.assertIsNone(value["health"]["regenerated"])  # the fixture has no feature states: unknown, not a pass
        (method, path, body, _), = self.writes()
        self.assertEqual(path, "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1")
        self.assertEqual(osb.parameter_value(body["feature"], "lengthValue")["expression"], "4 mm")
        self.assertEqual(body["sourceMicroversion"], "mc")
        self.assertTrue(all("/d/base/" not in c[1] or c[0] == "GET" for c in self.fake.calls))

    # Review findings, Onshape ------------------------------------------------

    def set_args(self, did="copy", wid="wc", assignments=None, name="Bracket-V001"):
        return {"document_id": did, "workspace_id": wid, "element_id": "ec", "element_kind": "PARTSTUDIO",
                "expected_document_name": name, "assignments": assignments or {"wall": "4 mm"}}

    def test_write_to_unprotected_original_refused(self):
        # Finding 1: no protect and no copy first. The original's name matches, and it is still refused.
        self.fake.routes[("GET", "/partstudios/d/base/w/wb/e/ec/features")] = self.fake.routes[("GET", "/partstudios/d/copy/w/wc/e/ec/features")]
        value, is_error = call(self.server, "onshape_set_variables", self.set_args("base", "wb", name="Bracket"))
        self.assertTrue(is_error)
        self.assertIn("not a copy this bridge made", value["error"])
        value, is_error = call(self.server, "onshape_create_version", {"document_id": "base", "workspace_id": "wb", "name": "v"})
        self.assertTrue(is_error)
        self.assertEqual(self.writes(), [])

    def test_copy_protects_its_source_and_only_its_own_workspace_is_writable(self):
        copy = self.make_copy()
        self.assertIn("base", osb.ProtectedDocuments(self.state).entries)
        self.assertTrue(copy["source_protected_by_this_call"])
        value, is_error = call(self.server, "onshape_set_variables", self.set_args(wid="another"))
        self.assertTrue(is_error)
        self.assertIn("not the workspace", value["error"])
        value, is_error = call(self.server, "onshape_copy_workspace", {"document_id": "copy", "workspace_id": "wc", "new_name": "x"})
        self.assertTrue(is_error)
        self.assertEqual(self.writes(), [])

    def test_corrupt_state_fails_closed(self):
        # Finding 2: an unreadable state file must not read as "nothing protected".
        self.make_copy()
        self.state.write_text("{not json", "utf-8")
        server = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=self.fake), osb.ProtectedDocuments(self.state))
        value, is_error = call(server, "onshape_set_variables", self.set_args())
        self.assertTrue(is_error)
        self.assertIn("cannot be read", value["error"])
        self.assertTrue(call(server, "onshape_copy_workspace", {"document_id": "base", "workspace_id": "wb", "new_name": "B"})[1])
        self.assertTrue(call(server, "onshape_protect_base", {"document_id": "base", "workspace_id": "wb"})[1])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.state.read_text("utf-8"), "{not json")  # left for a person to look at
        self.state.write_text(json.dumps({"format": 2, "protected": [], "copies": {}}), "utf-8")
        self.assertIsNotNone(osb.ProtectedDocuments(self.state).error)

    def test_state_is_written_atomically_and_reads_the_first_format(self):
        self.make_copy()
        data = json.loads(self.state.read_text("utf-8"))
        self.assertEqual(data["format"], 2)
        self.assertIn("copy", data["copies"])
        self.assertEqual(sorted(p.name for p in self.state.parent.iterdir()), ["protected.json"])
        self.state.write_text(json.dumps({"base": {"name": "Bracket", "workspace_id": "wb", "microversion": "m1"}}), "utf-8")
        legacy = osb.ProtectedDocuments(self.state)
        self.assertIsNone(legacy.error)
        self.assertIn("base", legacy.entries)
        self.assertEqual(legacy.copies, {})

    def test_missing_feature_status_is_unknown_not_regenerated(self):
        # Finding 3.
        self.assertIsNone(osb.feature_health({"features": [{"featureId": "f1"}], "featureStates": {}})["regenerated"])
        self.assertIsNone(osb.feature_health({})["regenerated"])
        self.assertIsNone(osb.feature_health(None)["regenerated"])
        self.assertIsNone(osb.feature_health({"features": [{"featureId": "f1"}, {"featureId": "f2"}],
                                              "featureStates": {"f1": {"featureStatus": "OK"}}})["regenerated"])
        self.assertIsNone(osb.feature_health({"featureStates": {"f1": {"featureStatus": "GREAT"}}})["regenerated"])
        self.assertIsNone(osb.feature_health({"featureStates": {"f1": {}}})["regenerated"])
        self.assertTrue(osb.feature_health({"features": [{"featureId": "f1"}], "featureStates": {"f1": {"featureStatus": "OK"}}})["regenerated"])
        self.fake.routes[("GET", "/partstudios/d/copy/w/wc/e/ec/features")] = {"features": [{"featureId": "f1"}], "featureStates": {}}
        value, _ = call(self.server, "onshape_read_properties", {"document_id": "copy", "wvm": "w", "wvm_id": "wc", "element_id": "ec"})
        self.assertIsNone(value["properties"])
        self.assertIn("unknown", value["note"])
        self.assertFalse(any("massproperties" in c[1] for c in self.fake.calls))

    def test_untouched_check_needs_real_microversions(self):
        # Finding 4: two missing values must not compare equal.
        self.fake.routes[("GET", "/documents/d/base/w/wb/currentmicroversion")] = {}
        value, is_error = call(self.server, "onshape_protect_base", {"document_id": "base", "workspace_id": "wb"})
        self.assertTrue(is_error)
        self.assertTrue(call(self.server, "onshape_copy_workspace", {"document_id": "base", "workspace_id": "wb", "new_name": "B"})[1])
        self.state.write_text(json.dumps({"format": 2, "protected": {"base": {"workspace_id": "wb"}}, "copies": {}}), "utf-8")
        server = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=self.fake), osb.ProtectedDocuments(self.state))
        value, is_error = call(server, "onshape_check_base_untouched", {"document_id": "base"})
        self.assertTrue(is_error)
        self.assertNotIn("untouched", value)
        self.fake.routes[("GET", "/documents/d/base/w/wb/currentmicroversion")] = {"microversion": "m1"}
        self.state.write_text(json.dumps({"format": 2, "protected": {"base": {"workspace_id": "wb", "microversion": "m1"}}, "copies": {}}), "utf-8")
        server = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=self.fake), osb.ProtectedDocuments(self.state))
        self.fake.routes[("GET", "/documents/d/base/w/wb/currentmicroversion")] = {"microversion": ""}
        self.assertTrue(call(server, "onshape_check_base_untouched", {"document_id": "base"})[1])

    def test_featurescript_uses_the_configuration(self):
        # Finding 5.
        self.fake.routes[("POST", "/partstudios/d/copy/w/wc/e/ec/featurescript")] = {"result": "box"}
        value, is_error = call(self.server, "onshape_eval_featurescript",
                               {"document_id": "copy", "wvm": "w", "wvm_id": "wc", "element_id": "ec", "script": "function(context, queries){ return 1; }",
                                "configuration": "Length%3D25%2Bmm"})
        self.assertFalse(is_error, value)
        self.assertEqual(value["configuration"], "Length%3D25%2Bmm")
        transport_urls = [c for c in self.fake.calls if c[1].endswith("/featurescript")]
        self.assertEqual(len(transport_urls), 1)
        self.assertIn("configuration=", self.fake.urls[-1])

    def test_batch_is_checked_before_any_write(self):
        self.make_copy()
        features = [variable_feature("v1", "wall", "3 mm"), {"featureType": "assignVariable", "featureId": "v9", "name": "odd",
                    "parameters": [{"parameterId": "name", "value": "odd"}]}]
        self.fake.routes[("GET", "/partstudios/d/copy/w/wc/e/ec/features")] = {"features": features, "featureStates": {}}
        value, is_error = call(self.server, "onshape_set_variables", self.set_args(assignments={"wall": "4 mm", "odd": "1"}))
        self.assertTrue(is_error)
        self.assertIn("Nothing was written", value["error"])
        self.assertEqual(self.writes(), [])

    def test_failed_write_part_way_is_undone_with_a_journal(self):
        self.make_copy()
        self.fake.routes[("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v1")] = {"sourceMicroversion": "mc2"}
        self.fake.routes[("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v2")] = (500, {"message": "boom"})
        value, is_error = call(self.server, "onshape_set_variables", self.set_args(assignments={"wall": "4 mm", "rib": "#wall * 3"}))
        self.assertTrue(is_error)
        self.assertIn("were undone", value["error"])
        self.assertIn('"undone": true', value["error"])
        posts = [(c[1].rsplit("/", 1)[1], c[2]) for c in self.writes()]
        self.assertEqual([fid for fid, _ in posts], ["v1", "v2", "v1"])
        self.assertEqual(posts[1][1]["sourceMicroversion"], "mc2")  # chained from the first write's answer
        self.assertEqual(osb.parameter_value(posts[2][1]["feature"], "lengthValue")["expression"], "3 mm")

    def test_failed_undo_retires_the_copy(self):
        self.make_copy()
        self.fake.routes[("POST", "/partstudios/d/copy/w/wc/e/ec/features/featureid/v2")] = (500, {})
        answers = {"n": 0}
        original = self.fake.__call__

        def flaky(method, url, headers, body):
            if url.split("?")[0].endswith("/featureid/v1") and method == "POST":
                answers["n"] += 1
                if answers["n"] > 1:
                    return 500, {}, b"{}"
            return original(method, url, headers, body)

        server = osb.build_server(lambda: osb.OnshapeClient("a", "s", transport=flaky), osb.ProtectedDocuments(self.state))
        value, is_error = call(server, "onshape_set_variables", self.set_args(assignments={"wall": "4 mm", "rib": "#wall * 3"}))
        self.assertTrue(is_error)
        self.assertIn("unknown state", value["error"])
        value, is_error = call(server, "onshape_set_variables", self.set_args())
        self.assertTrue(is_error)
        self.assertIn("Discard it", value["error"])

    def test_set_variables_reports_failed_regeneration(self):
        self.make_copy()
        self.fake.routes[("GET", "/partstudios/d/copy/w/wc/e/ec/features")] = {
            "features": FEATURES, "featureStates": {"v1": {"featureStatus": "OK"}, "v2": {"featureStatus": "OK"},
                                                     "v3": {"featureStatus": "OK"}, "e1": {"featureStatus": "ERROR"}},
            "sourceMicroversion": "mc",
        }
        value, is_error = call(self.server, "onshape_set_variables", self.set_args())
        self.assertFalse(is_error, value)
        self.assertFalse(value["health"]["regenerated"])
        self.assertEqual([f["feature_id"] for f in value["features"]], ["v1", "v2", "v3", "e1"])

    def test_read_properties_skips_measuring_a_failed_regeneration(self):
        self.fake.routes[("GET", "/partstudios/d/copy/w/wc/e/ec/features")] = {
            "features": [], "featureStates": {"f": {"featureStatus": "ERROR"}}
        }
        value, _ = call(self.server, "onshape_read_properties", {"document_id": "copy", "wvm": "w", "wvm_id": "wc", "element_id": "ec"})
        self.assertIsNone(value["properties"])
        self.assertFalse(any("massproperties" in c[1] for c in self.fake.calls))


# ---------------------------------------------------------------------------
# SOLIDWORKS
# ---------------------------------------------------------------------------


class SolidWorksHelperTests(unittest.TestCase):
    def test_parse_equation(self):
        self.assertEqual(
            swb.parse_equation('"D1@Boss-Extrude1" = "wall" * 2'),
            {"target": "D1@Boss-Extrude1", "references": ["wall"], "expression": '"wall" * 2'},
        )
        self.assertIsNone(swb.parse_equation("no equals sign"))

    def test_global_variable_drivers(self):
        equations = [
            {"text": '"wall" = 3mm', "global_variable": True},
            {"text": '"rib" = "wall" * 2', "global_variable": True},
            {"text": '"unused" = 1mm', "global_variable": True},
            {"text": '"D1@Boss-Extrude1" = "rib"', "global_variable": False},
        ]
        drivers = swb.global_variable_drivers(equations)
        self.assertEqual(drivers["wall"]["drives_dimensions"], ["D1@Boss-Extrude1"])
        self.assertTrue(drivers["rib"]["drives_geometry"])
        self.assertFalse(drivers["unused"]["drives_geometry"])

    def test_check_new_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            protected = swb.ProtectedFiles(Path(tmp) / "p.json")
            existing = Path(tmp) / "a.SLDPRT"
            existing.write_bytes(b"x")
            for bad in ("relative.SLDPRT", str(Path(tmp) / "a.step"), str(existing), str(Path(tmp) / "nope" / "b.SLDPRT")):
                with self.assertRaises(mcp_stdio.ToolError, msg=bad):
                    swb.check_new_path(bad, protected)
            self.assertEqual(swb.check_new_path(str(Path(tmp) / "b.sldprt"), protected), Path(tmp) / "b.sldprt")


class FakeDoc:
    def __init__(self, path, title, dirty=False, kind=swb.SW_DOC_PART):
        self.path, self.title, self.dirty, self.kind = path, title, dirty, kind

    def GetPathName(self):
        return self.path

    def GetTitle(self):
        return self.title

    def GetSaveFlag(self):
        return self.dirty

    def GetType(self):
        return self.kind


class FakeSession:
    """Stands in for SolidWorksSession: same methods, no COM."""

    def __init__(self, doc):
        self.doc = doc
        self.app = self
        self.sets = []
        self.dims = [
            {"name": "D1@Boss-Extrude1", "value_si": 0.003, "driving": True, "read_only": False},
            {"name": "D2@Boss-Extrude1", "value_si": 0.02, "driving": True, "read_only": False},
            {"name": "RD1@Annotations", "value_si": 0.01, "driving": False, "read_only": False},
        ]
        self.eqs = [{"index": 0, "text": '"wall" = 3mm', "value": 0.003, "global_variable": True}]
        self.rebuild_ok = True
        self.feature_failures = []
        self.refuse = {}
        self.fail_undo = False

    @property
    def ActiveDoc(self):
        return self.doc

    def revision(self):
        return "33.0.0"

    def active_doc(self):
        return self.doc

    def describe(self, doc):
        return {"path": doc.path, "title": doc.title, "material": "AISI 1020"}

    def dimensions(self, doc):
        return self.dims

    def equations(self, doc):
        return [dict(e) for e in self.eqs]

    def feature_list(self, doc):
        return [{"name": "Boss-Extrude1", "type": "Extrusion"}, {"name": "Sketch1", "type": "ProfileFeature"}]

    def feature_errors(self, doc):
        return list(self.feature_failures)

    def set_dimension(self, doc, name, value):
        if name in self.refuse:
            return self.refuse[name]
        for dim in self.dims:
            if dim["name"] == name:
                if self.fail_undo and value == dim["value_si"]:
                    raise RuntimeError("COM call failed")
                self.sets.append((doc.path, name, value))
        return "Successful"

    def find_global(self, doc, name):
        for e in self.eqs:
            if e["global_variable"] and swb.parse_equation(e["text"])["target"] == name:
                return {"index": e["index"], "text": e["text"]}
        return None

    def put_equation(self, doc, index, text):
        self.sets.append((doc.path, f"equation {index}", text))
        self.eqs[index]["text"] = text

    def rebuild(self, doc):
        return self.rebuild_ok

    def save_copy(self, doc, new_path):
        Path(new_path).write_bytes(Path(doc.path).read_bytes())
        return {"ok": True, "errors": 0, "warnings": 0}

    def open_and_activate(self, path, kind):
        self.doc = FakeDoc(path, Path(path).name, kind=kind)
        return self.doc

    def save(self, doc):
        return {"ok": True, "errors": 0, "warnings": 0}


class SolidWorksGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.base = self.dir / "Bracket.SLDPRT"
        self.base.write_bytes(b"solid part bytes")
        self.session = FakeSession(FakeDoc(str(self.base), "Bracket.SLDPRT"))
        self.state = self.dir / "state.json"
        self.server = swb.build_server(lambda: self.session, swb.ProtectedFiles(self.state))

    def tearDown(self):
        self.tmp.cleanup()

    def test_protect_refuses_unsaved_changes(self):
        self.session.doc.dirty = True
        value, is_error = call(self.server, "sw_protect_base")
        self.assertTrue(is_error)
        self.assertIn("unsaved changes", value["error"])

    def test_full_variant_flow_never_writes_base(self):
        self.assertFalse(call(self.server, "sw_protect_base")[1])
        # A write aimed at the base is refused even when the caller names it.
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(self.base), "dimensions": {"D1@Boss-Extrude1": 0.004}})
        self.assertTrue(is_error)
        self.assertIn("protected base", value["error"])
        # Same file name in another folder is refused: SOLIDWORKS could not open both.
        (self.dir / "other").mkdir()
        value, is_error = call(self.server, "sw_save_copy_and_open", {"new_path": str(self.dir / "other" / "Bracket.SLDPRT")})
        self.assertTrue(is_error)
        copy = self.dir / "Bracket-V001.SLDPRT"
        value, is_error = call(self.server, "sw_save_copy_and_open", {"new_path": str(copy)})
        self.assertFalse(is_error, value)
        self.assertTrue(value["active_is_copy"])
        # A write naming the base while the copy is active is refused too.
        self.assertTrue(call(self.server, "sw_apply_parameters", {"expected_path": str(self.base), "global_variables": {"wall": "4mm"}})[1])
        value, is_error = call(
            self.server, "sw_apply_parameters",
            {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.004}, "global_variables": {"wall": "4mm"}},
        )
        self.assertFalse(is_error, value)
        self.assertTrue(value["health"]["regenerated"])
        self.assertTrue(all(path == str(copy) for path, _, _ in self.session.sets))
        self.assertFalse(call(self.server, "sw_save_active", {"expected_path": str(copy)})[1])
        self.assertTrue(call(self.server, "sw_check_base_untouched", {"path": str(self.base)})[0]["untouched"])

    def test_driven_dimension_refused(self):
        copy = self.dir / "Bracket-V002.SLDPRT"
        call(self.server, "sw_save_copy_and_open", {"new_path": str(copy)})
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"RD1@Annotations": 0.02}})
        self.assertTrue(is_error)
        self.assertIn("not a driving dimension", value["error"])
        self.assertEqual(self.session.sets, [])

    def test_base_change_detected(self):
        call(self.server, "sw_protect_base")
        self.base.write_bytes(b"someone saved over it")
        self.assertFalse(call(self.server, "sw_check_base_untouched", {"path": str(self.base)})[0]["untouched"])

    # Review findings, SOLIDWORKS ---------------------------------------------

    def make_copy(self, name="Bracket-V001.SLDPRT"):
        copy = self.dir / name
        value, is_error = call(self.server, "sw_save_copy_and_open", {"new_path": str(copy)})
        self.assertFalse(is_error, value)
        return copy

    def test_write_to_unprotected_original_refused(self):
        # Finding 1: the original was never protected or copied; writes are refused anyway.
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(self.base), "dimensions": {"D1@Boss-Extrude1": 0.004}})
        self.assertTrue(is_error)
        self.assertIn("not a copy this bridge made", value["error"])
        self.assertTrue(call(self.server, "sw_save_active", {"expected_path": str(self.base)})[1])
        self.assertEqual(self.session.sets, [])
        # Any other saved file that the bridge did not make is refused the same way.
        other = self.dir / "Other.SLDPRT"
        other.write_bytes(b"x")
        self.session.doc = FakeDoc(str(other), "Other.SLDPRT")
        self.assertTrue(call(self.server, "sw_apply_parameters", {"expected_path": str(other), "dimensions": {"D1@Boss-Extrude1": 0.004}})[1])
        self.assertEqual(self.session.sets, [])

    def test_copy_protects_the_original_itself(self):
        copy = self.dir / "Bracket-V001.SLDPRT"
        value, is_error = call(self.server, "sw_save_copy_and_open", {"new_path": str(copy)})
        self.assertFalse(is_error, value)
        self.assertTrue(value["source_protected_by_this_call"])
        self.assertTrue(call(self.server, "sw_check_base_untouched", {"path": str(self.base)})[0]["untouched"])
        # A copy of a copy is refused: every variant starts from the base.
        self.assertTrue(call(self.server, "sw_save_copy_and_open", {"new_path": str(self.dir / "V002.SLDPRT")})[1])

    def test_failed_rebuild_is_a_failed_variant(self):
        # Finding 2: no feature shows an error, but SOLIDWORKS says the rebuild failed.
        copy = self.make_copy()
        self.session.rebuild_ok = False
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.004}})
        self.assertFalse(is_error, value)
        self.assertFalse(value["health"]["regenerated"])
        self.assertFalse(value["health"]["rebuild_reported_success"])
        value, _ = call(self.server, "sw_read_properties")
        self.assertIsNone(value["properties"])
        # A later rebuild that succeeds clears it.
        self.session.rebuild_ok = True
        value, _ = call(self.server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.005}})
        self.assertTrue(value["health"]["regenerated"])

    def test_corrupt_state_fails_closed(self):
        # Finding 3: unreadable protection data must not read as "nothing protected".
        copy = self.make_copy()
        self.state.write_text("{not json", "utf-8")
        server = swb.build_server(lambda: self.session, swb.ProtectedFiles(self.state))
        value, is_error = call(server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.004}})
        self.assertTrue(is_error)
        self.assertIn("cannot be read", value["error"])
        self.session.doc = FakeDoc(str(self.base), "Bracket.SLDPRT")
        self.assertTrue(call(server, "sw_save_copy_and_open", {"new_path": str(self.dir / "V009.SLDPRT")})[1])
        self.assertTrue(call(server, "sw_protect_base")[1])
        self.assertFalse((self.dir / "V009.SLDPRT").exists())
        self.assertEqual(self.session.sets, [])
        for bad in ("[]", json.dumps({"format": 2, "protected": {}}), json.dumps({"format": 9}), json.dumps({"x": 1})):
            self.state.write_text(bad, "utf-8")
            self.assertIsNotNone(swb.ProtectedFiles(self.state).error, bad)

    def test_state_is_written_atomically_and_reads_the_first_format(self):
        self.make_copy()
        data = json.loads(self.state.read_text("utf-8"))
        self.assertEqual(data["format"], 2)
        self.assertEqual(len(data["copies"]), 1)
        self.assertEqual(len(data["protected"]), 1)
        self.assertFalse(any(p.name.endswith((".tmp", ".lock")) for p in self.dir.iterdir()))
        key = swb.normalise(str(self.base))
        self.state.write_text(json.dumps({key: data["protected"][key]}), "utf-8")
        legacy = swb.ProtectedFiles(self.state)
        self.assertIsNone(legacy.error)
        self.assertTrue(legacy.is_protected(str(self.base)))

    def test_state_lock_waits_for_another_writer(self):
        lock = self.dir / "state.json.lock"
        lock.write_text("1")
        protected = swb.ProtectedFiles(self.state)
        original = swb.StateLock.__init__

        def quick(this, path, timeout=0.2, stale_after=120.0):
            original(this, path, timeout, stale_after)

        swb.StateLock.__init__ = quick
        try:
            with self.assertRaises(mcp_stdio.ToolError):
                protected.add(str(self.base), {"sha256": "x"})
        finally:
            swb.StateLock.__init__ = original
        lock.unlink()
        protected.add(str(self.base), {"sha256": "x"})
        self.assertTrue(swb.ProtectedFiles(self.state).is_protected(str(self.base)))

    def test_whole_batch_checked_before_any_change(self):
        copy = self.make_copy()
        cases = [
            {"dimensions": {"D1@Boss-Extrude1": 0.004, "nope@Sketch1": 0.1}},
            {"dimensions": {"D1@Boss-Extrude1": 0.004}, "global_variables": {"missing": "2mm"}},
            {"dimensions": {"D1@Boss-Extrude1": "4mm"}},
            {"dimensions": {"D1@Boss-Extrude1": float("nan")}},
        ]
        for args in cases:
            value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(copy), **args})
            self.assertTrue(is_error, args)
            self.assertIn("Nothing was changed", value["error"])
        self.session.eqs.append({"index": 1, "text": '"D2@Boss-Extrude1" = "wall" * 2', "value": 0.006, "global_variable": False})
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"D2@Boss-Extrude1": 0.03}})
        self.assertTrue(is_error)
        self.assertIn("set by an equation", value["error"])
        self.assertEqual(self.session.sets, [])

    def test_failed_change_part_way_is_undone(self):
        copy = self.make_copy()
        self.session.refuse = {"D2@Boss-Extrude1": "InvalidValue"}
        value, is_error = call(
            self.server, "sw_apply_parameters",
            {"expected_path": str(copy), "global_variables": {"wall": "4mm"}, "dimensions": {"D1@Boss-Extrude1": 0.004, "D2@Boss-Extrude1": -1.0}},
        )
        self.assertTrue(is_error)
        self.assertIn("were undone", value["error"])
        self.assertEqual(self.session.eqs[0]["text"], '"wall" = 3mm')
        self.assertEqual(self.session.sets[-1], (str(copy), "equation 0", '"wall" = 3mm'))
        self.assertEqual(self.session.sets[-2], (str(copy), "D1@Boss-Extrude1", 0.003))
        # The copy is still usable.
        self.session.refuse = {}
        self.assertFalse(call(self.server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.004}})[1])

    def test_failed_undo_retires_the_copy(self):
        copy = self.make_copy()
        self.session.refuse = {"D2@Boss-Extrude1": "InvalidValue"}
        self.session.fail_undo = True
        value, is_error = call(
            self.server, "sw_apply_parameters",
            {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.004, "D2@Boss-Extrude1": -1.0}},
        )
        self.assertTrue(is_error)
        self.assertIn("unknown state", value["error"])
        self.session.refuse, self.session.fail_undo = {}, False
        value, is_error = call(self.server, "sw_apply_parameters", {"expected_path": str(copy), "dimensions": {"D1@Boss-Extrude1": 0.004}})
        self.assertTrue(is_error)
        self.assertIn("Discard it", value["error"])
        self.assertIn("retired", json.loads(self.state.read_text("utf-8"))["copies"][swb.normalise(str(copy))])

    def test_read_parameters_lists_features_and_says_what_it_cannot_check(self):
        value, is_error = call(self.server, "sw_read_parameters")
        self.assertFalse(is_error, value)
        self.assertEqual([f["type"] for f in value["features"]], ["Extrusion", "ProfileFeature"])
        self.assertIn("not_checked", value["integrity_note"])
        self.assertIn("unknown", value["integrity_note"])

    def test_untouched_check_when_the_base_is_gone(self):
        self.make_copy()
        self.base.unlink()
        value, is_error = call(self.server, "sw_check_base_untouched", {"path": str(self.base)})
        self.assertFalse(is_error, value)
        self.assertFalse(value["untouched"])

    def test_missing_material_means_no_mass(self):
        self.session.describe = lambda doc: {"path": doc.path, "material": ""}
        self.session.properties = lambda doc: {"mass_kg": 0.2, "volume_m3": 1e-4}
        value, is_error = call(self.server, "sw_read_properties")
        self.assertFalse(is_error, value)
        self.assertIsNone(value["properties"]["mass_kg"])


if __name__ == "__main__":
    unittest.main()
