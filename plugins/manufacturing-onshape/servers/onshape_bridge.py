#!/usr/bin/env python3
"""Onshape bridge: the CAD adapter surface of manufacturing-onshape, over MCP stdio.

Implements the operations `skills/design-space-explore/reference/cad-adapter.md`
lists, against Onshape's public REST API. Standard library only.

Credentials come from the environment and never from the conversation:

    ONSHAPE_ACCESS_KEY, ONSHAPE_SECRET_KEY   API keys (Onshape: My Account > Developer)
    ONSHAPE_BASE_URL                         default https://cad.onshape.com (Enterprise: https://<company>.onshape.com)
    ONSHAPE_API_VERSION                      default v10
    ONSHAPE_BRIDGE_STATE                     file that remembers protected base documents
                                             default ~/.manufacturing-onshape/protected.json

Status: written against Onshape's documentation and its generated client's
endpoint list. NOT yet run against a live Onshape account. Every tool says so
in its description until it has been.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_stdio import Server, ToolError, log  # noqa: E402

VERSION = "0.1.0"
UNVERIFIED = " (Not yet run against a live Onshape account.)"

# Feature parameter ids that hold a Part Studio variable's value, by variable type.
VARIABLE_VALUE_PARAMETER_IDS = ("lengthValue", "angleValue", "numberValue", "anyValue", "value")
REFERENCE_PATTERN = re.compile(r"#([A-Za-z_][A-Za-z0-9_]*)")


# ---------------------------------------------------------------------------
# Signing and transport
# ---------------------------------------------------------------------------


def make_nonce(length: int = 25) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def http_date(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%a, %d %b %Y %H:%M:%S GMT")


def sign_request(
    method: str,
    path: str,
    query: str,
    content_type: str,
    access_key: str,
    secret_key: str,
    nonce: str,
    date: str,
) -> str:
    """The `Authorization` header for Onshape's request-signature scheme.

    Per https://onshape-public.github.io/docs/auth/apikeys/ : HMAC-SHA256 with
    the secret key over method, nonce, date, content type, path and query,
    each followed by a newline, lower-cased; base64; sent as
    `On <access key>:HmacSHA256:<signature>`.
    """
    payload = "\n".join([method, nonce, date, content_type, path, query]) + "\n"
    digest = hmac.new(secret_key.encode("utf-8"), payload.lower().encode("utf-8"), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("utf-8")
    return f"On {access_key}:HmacSHA256:{signature}"


class OnshapeError(ToolError):
    pass


Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read() or b""


class OnshapeClient:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        base_url: str = "https://cad.onshape.com",
        api_version: str = "v10",
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not access_key or not secret_key:
            raise OnshapeError(
                "ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY are not set in the environment this "
                "connector runs in. Create an API key in Onshape (My Account > Developer, or Company "
                "Settings > Developer) and set both variables before starting Claude. Never paste the "
                "keys into the conversation."
            )
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.transport = transport
        self.sleep = sleep
        self.calls = 0

    def request(self, method: str, endpoint: str, query: Optional[Dict[str, Any]] = None, body: Any = None) -> Any:
        path = f"/api/{self.api_version}{endpoint}"
        query_string = urllib.parse.urlencode(query or {}, doseq=True)
        data = None if body is None else json.dumps(body).encode("utf-8")
        content_type = "application/json"
        for attempt in range(2):
            nonce = make_nonce()
            date = http_date()
            headers = {
                "Content-Type": content_type,
                "Accept": "application/json;charset=UTF-8; qs=0.09",
                "Date": date,
                "On-Nonce": nonce,
                "Authorization": sign_request(
                    method, path, query_string, content_type, self.access_key, self.secret_key, nonce, date
                ),
            }
            url = self.base_url + path + (f"?{query_string}" if query_string else "")
            status, response_headers, raw = self.transport(method, url, headers, data)
            self.calls += 1
            if status == 429 and attempt == 0:
                wait = float(_header(response_headers, "Retry-After") or 5)
                log(f"Onshape rate limit; waiting {wait}s")
                self.sleep(min(wait, 60))
                continue
            return self._parse(status, raw, method, path)
        raise OnshapeError("Onshape kept answering 429 Too Many Requests. Wait and try again later.")

    @staticmethod
    def _parse(status: int, raw: bytes, method: str, path: str) -> Any:
        if 200 <= status < 300:
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                return raw.decode("utf-8", "replace")
        detail = raw.decode("utf-8", "replace")[:500]
        if status == 401:
            raise OnshapeError("Onshape rejected the API key (401). Check the keys and that they belong to this Onshape stack.")
        if status == 402:
            raise OnshapeError(
                "Onshape says the account's annual API call limit is reached (402). No further calls will "
                "succeed until the limit resets or the plan changes."
            )
        if status == 403:
            raise OnshapeError(f"Onshape refused {method} {path} (403). A read-only key cannot write; otherwise the key's user has no access. {detail}")
        if status == 404:
            raise OnshapeError(f"Onshape found nothing at {method} {path} (404). Check the document, workspace and element ids. {detail}")
        raise OnshapeError(f"Onshape answered {status} to {method} {path}: {detail}")


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def iter_expressions(node: Any) -> Iterable[str]:
    """Every string held under an `expression` key, anywhere in a feature tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "expression" and isinstance(value, str):
                yield value
            else:
                yield from iter_expressions(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_expressions(item)


def parameter_value(feature: Dict[str, Any], parameter_id: str) -> Optional[Dict[str, Any]]:
    for parameter in feature.get("parameters", []) or []:
        if parameter.get("parameterId") == parameter_id:
            return parameter
    return None


def variable_features(features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Part Studio `#variable` features: name, expression and feature id."""
    found = []
    for feature in features:
        if feature.get("featureType") != "assignVariable":
            continue
        name_parameter = parameter_value(feature, "name") or {}
        value_parameter = None
        for parameter_id in VARIABLE_VALUE_PARAMETER_IDS:
            value_parameter = parameter_value(feature, parameter_id)
            if value_parameter and "expression" in value_parameter:
                break
        found.append(
            {
                "name": name_parameter.get("value"),
                "expression": (value_parameter or {}).get("expression"),
                "value_parameter_id": (value_parameter or {}).get("parameterId"),
                "feature_id": feature.get("featureId"),
                "feature_name": feature.get("name"),
            }
        )
    return found


def references_by_variable(features: List[Dict[str, Any]], names: Iterable[str]) -> Dict[str, List[str]]:
    """For each variable name, the features whose expressions reference `#name`.

    A variable feature's own definition is not a reference to itself.
    """
    wanted = set(names)
    references: Dict[str, List[str]] = {name: [] for name in wanted}
    for feature in features:
        own_name = None
        if feature.get("featureType") == "assignVariable":
            own_name = (parameter_value(feature, "name") or {}).get("value")
        label = feature.get("name") or feature.get("featureId") or "?"
        seen = set()
        for expression in iter_expressions(feature.get("parameters", [])):
            for match in REFERENCE_PATTERN.findall(expression):
                if match == own_name:
                    continue  # a #variable feature is not a reference to itself
                if match in wanted and match not in seen:
                    seen.add(match)
                    references[match].append(label)
    return references


def driving_variables(features: List[Dict[str, Any]], names: Iterable[str]) -> Dict[str, bool]:
    """Whether each variable reaches geometry: referenced by a feature that is not
    itself a #variable, or by a #variable that does (followed transitively)."""
    names = list(names)
    variable_feature_names = {
        f.get("name") or f.get("featureId"): (parameter_value(f, "name") or {}).get("value")
        for f in features
        if f.get("featureType") == "assignVariable"
    }
    references = references_by_variable(features, set(names) | set(v for v in variable_feature_names.values() if v))
    drives: Dict[str, bool] = {}

    def visit(name: str, trail: frozenset) -> bool:
        if name in drives:
            return drives[name]
        if name in trail:
            return False
        result = False
        for label in references.get(name, []):
            via_variable = variable_feature_names.get(label)
            if via_variable is None or visit(via_variable, trail | {name}):
                result = True
                break
        drives[name] = result
        return result

    return {name: visit(name, frozenset()) for name in names}


def feature_health(features_response: Dict[str, Any]) -> Dict[str, Any]:
    """Split the feature states into failed, warning and ok.

    `featureStatus` takes OK, INFO, WARNING, ERROR or UNKNOWN (GBTNodeStatusType).
    ERROR and UNKNOWN fail a variant; WARNING is reported and does not.
    """
    names = {f.get("featureId"): f.get("name") for f in features_response.get("features", []) or []}
    states = features_response.get("featureStates", {}) or {}
    failed, warnings = [], []
    for feature_id, state in states.items():
        status = (state or {}).get("featureStatus", "UNKNOWN")
        if (state or {}).get("inactive"):
            continue
        entry = {"feature_id": feature_id, "feature_name": names.get(feature_id), "status": status}
        if status in ("ERROR", "UNKNOWN"):
            failed.append(entry)
        elif status == "WARNING":
            warnings.append(entry)
    return {
        "regenerated": not failed,
        "failed_features": failed,
        "warning_features": warnings,
        "feature_count": len(states),
    }


def summarize_mass(mass_response: Any) -> Dict[str, Any]:
    """Mass and volume per body, never inventing a mass where Onshape has none.

    Onshape returns, per body, value triples [value, min, max] in SI units.
    A body with no material reports `hasMass: false`; its mass is absent, not zero.
    """
    bodies = (mass_response or {}).get("bodies", {}) if isinstance(mass_response, dict) else {}
    out = {}
    for body_id, body in bodies.items():
        has_mass = bool(body.get("hasMass"))
        out[body_id] = {
            "has_mass": has_mass,
            "mass_kg": _first(body.get("mass")) if has_mass else None,
            "volume_m3": _first(body.get("volume")),
            "mass_note": None if has_mass else "No material assigned: mass is unmeasured, not zero.",
        }
    return out


def _first(triple: Any) -> Optional[float]:
    if isinstance(triple, list) and triple:
        return triple[0]
    if isinstance(triple, (int, float)):
        return triple
    return None


class ProtectedDocuments:
    """Documents read as a base model. Writes to them are refused.

    Persisted to a small JSON file so a restarted connector still refuses.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text("utf-8"))
            except ValueError:
                log(f"Ignoring unreadable state file {path}")

    def add(self, document_id: str, record: Dict[str, Any]) -> None:
        self.entries[document_id] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2), "utf-8")

    def check_writable(self, document_id: str) -> None:
        if document_id in self.entries:
            name = self.entries[document_id].get("name")
            raise ToolError(
                f"Refused: document {document_id} ({name}) is a protected base model. Variants and estimating "
                "models are written to a copy, never the base. Copy the workspace to a new document first."
            )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

ID = {"type": "string", "minLength": 1}
WVM = {"type": "string", "enum": ["w", "v", "m"], "description": "w workspace, v version, m microversion"}


def build_server(client_factory: Callable[[], OnshapeClient], protected: ProtectedDocuments) -> Server:
    server = Server(
        name="onshape-bridge",
        version=VERSION,
        instructions=(
            "CAD adapter for manufacturing-onshape. Read cad-adapter.md before the first call. "
            "Protect the base document before any write; every write goes to a copied document."
        ),
    )
    state: Dict[str, Optional[OnshapeClient]] = {"client": None}

    def client() -> OnshapeClient:
        if state["client"] is None:
            state["client"] = client_factory()
        return state["client"]

    def wvm_path(args: Dict[str, Any]) -> str:
        return f"/d/{args['document_id']}/{args['wvm']}/{args['wvm_id']}/e/{args['element_id']}"

    @server.tool(
        "onshape_status",
        "Check the connector: whether API keys are set (never their values), which Onshape stack, and who the key acts as." + UNVERIFIED,
    )
    def status(_: Dict[str, Any]) -> Dict[str, Any]:
        info = client().request("GET", "/users/sessioninfo")
        return {
            "base_url": client().base_url,
            "api_version": client().api_version,
            "acting_as": {k: (info or {}).get(k) for k in ("name", "id")},
            "protected_documents": len(protected.entries),
            "api_calls_this_session": client().calls,
        }

    @server.tool(
        "onshape_read_document",
        "Read a document's name, workspaces and tabs (elements). Read-only." + UNVERIFIED,
        {"document_id": ID, "workspace_id": {"type": "string", "description": "Defaults to the document's default workspace"}},
        ["document_id"],
    )
    def read_document(args: Dict[str, Any]) -> Dict[str, Any]:
        did = args["document_id"]
        document = client().request("GET", f"/documents/{did}")
        workspaces = client().request("GET", f"/documents/d/{did}/workspaces") or []
        wid = args.get("workspace_id") or ((document or {}).get("defaultWorkspace") or {}).get("id")
        elements = client().request("GET", f"/documents/d/{did}/w/{wid}/elements") if wid else []
        return {
            "document_id": did,
            "name": (document or {}).get("name"),
            "default_workspace_id": ((document or {}).get("defaultWorkspace") or {}).get("id"),
            "workspaces": [{"id": w.get("id"), "name": w.get("name")} for w in workspaces],
            "elements": [
                {"id": e.get("id"), "name": e.get("name"), "type": e.get("elementType")} for e in (elements or [])
            ],
            "protected": did in protected.entries,
        }

    @server.tool(
        "onshape_protect_base",
        "Mark a document as a base model: record its name and current microversion, and refuse every write "
        "to it from now on. Call before the first write of any run that starts from an existing model." + UNVERIFIED,
        {"document_id": ID, "workspace_id": ID},
        ["document_id", "workspace_id"],
    )
    def protect_base(args: Dict[str, Any]) -> Dict[str, Any]:
        did, wid = args["document_id"], args["workspace_id"]
        document = client().request("GET", f"/documents/{did}") or {}
        microversion = client().request("GET", f"/documents/d/{did}/w/{wid}/currentmicroversion") or {}
        record = {
            "name": document.get("name"),
            "workspace_id": wid,
            "microversion": microversion.get("microversion"),
            "protected_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        protected.add(did, record)
        return {"document_id": did, **record}

    @server.tool(
        "onshape_check_base_untouched",
        "Confirm a protected base document's workspace is still at the microversion recorded when it was protected. "
        "A moved microversion means someone or something edited the base: stop and tell the user." + UNVERIFIED,
        {"document_id": ID},
        ["document_id"],
    )
    def check_untouched(args: Dict[str, Any]) -> Dict[str, Any]:
        did = args["document_id"]
        record = protected.entries.get(did)
        if not record:
            raise ToolError(f"Document {did} was never protected, so there is no recorded microversion to compare.")
        now = client().request("GET", f"/documents/d/{did}/w/{record['workspace_id']}/currentmicroversion") or {}
        current = now.get("microversion")
        return {
            "document_id": did,
            "recorded_microversion": record.get("microversion"),
            "current_microversion": current,
            "untouched": current == record.get("microversion"),
        }

    @server.tool(
        "onshape_read_parameters",
        "Read what can be varied in a Part Studio: its #variable features, the variables of a Variable Studio if "
        "given, and its configuration inputs. For each variable, list the Part Studio features whose expressions "
        "reference it. A variable no feature references does not drive geometry. Read-only." + UNVERIFIED,
        {
            "document_id": ID,
            "wvm": WVM,
            "wvm_id": ID,
            "element_id": {"type": "string", "description": "The Part Studio"},
            "variable_studio_element_id": {"type": "string", "description": "Optional Variable Studio in the same document"},
        },
        ["document_id", "wvm", "wvm_id", "element_id"],
    )
    def read_parameters(args: Dict[str, Any]) -> Dict[str, Any]:
        features_response = client().request("GET", "/partstudios" + wvm_path(args) + "/features") or {}
        features = features_response.get("features", []) or []
        part_studio_variables = variable_features(features)
        studio_variables: List[Dict[str, Any]] = []
        if args.get("variable_studio_element_id"):
            tables = client().request(
                "GET",
                f"/variables/d/{args['document_id']}/{args['wvm']}/{args['wvm_id']}/e/{args['variable_studio_element_id']}/variables",
            ) or []
            for table in tables:
                for variable in table.get("variables", []) or []:
                    studio_variables.append(
                        {k: variable.get(k) for k in ("name", "type", "expression", "value", "description")}
                    )
        configuration = client().request(
            "GET", f"/elements/d/{args['document_id']}/{args['wvm']}/{args['wvm_id']}/e/{args['element_id']}/configuration"
        ) or {}
        names = [v["name"] for v in part_studio_variables + studio_variables if v.get("name")]
        references = references_by_variable(features, names)
        drives = driving_variables(features, names)

        def annotate(variable: Dict[str, Any]) -> Dict[str, Any]:
            name = variable.get("name")
            return dict(variable, referenced_by=references.get(name, []), drives_geometry=drives.get(name, False))

        return {
            "part_studio_variables": [annotate(v) for v in part_studio_variables],
            "variable_studio_variables": [annotate(v) for v in studio_variables],
            "configuration_inputs": [
                {
                    "parameter_id": p.get("parameterId"),
                    "name": p.get("parameterName"),
                    "type": p.get("btType"),
                    "default": p.get("defaultValue"),
                }
                for p in configuration.get("configurationParameters", []) or []
            ],
            "health": feature_health(features_response),
            "note": (
                "drives_geometry is read from expressions: a variable referenced by no geometry feature, directly "
                "or through another variable, does not drive this Part Studio. It does not see references from "
                "other tabs or from FeatureScript code."
            ),
        }

    @server.tool(
        "onshape_read_properties",
        "Regeneration health, mass, volume and bounding box of a Part Studio, optionally for one configuration "
        "(pass the queryParam from onshape_encode_configuration). Mass is absent, not zero, where no material is "
        "assigned. Onshape documents its bounding boxes as approximate. Read-only." + UNVERIFIED,
        {
            "document_id": ID,
            "wvm": WVM,
            "wvm_id": ID,
            "element_id": ID,
            "configuration": {"type": "string", "description": "Encoded configuration, e.g. the value after configuration="},
        },
        ["document_id", "wvm", "wvm_id", "element_id"],
    )
    def read_properties(args: Dict[str, Any]) -> Dict[str, Any]:
        query = {"configuration": args["configuration"]} if args.get("configuration") else None
        base = "/partstudios" + wvm_path(args)
        features_response = client().request("GET", base + "/features", query) or {}
        health = feature_health(features_response)
        if not health["regenerated"]:
            return {"health": health, "properties": None, "note": "Failed regeneration: not measured."}
        mass = client().request("GET", base + "/massproperties", query)
        boxes = client().request("GET", base + "/boundingboxes", query)
        return {
            "health": health,
            "mass_properties": summarize_mass(mass),
            "bounding_box_m": boxes,
            "bounding_box_note": "Onshape documents these boxes as approximate. Use onshape_eval_featurescript with evBox3d tight for a check against a hand calculation.",
        }

    @server.tool(
        "onshape_encode_configuration",
        "Encode configuration input values for use with read calls. Evaluating a configuration writes nothing, "
        "so a sweep over configuration inputs needs no copied document. Read-only." + UNVERIFIED,
        {
            "document_id": ID,
            "element_id": ID,
            "parameters": {"type": "object", "description": "parameterId -> value, e.g. {\"Length\": \"25 mm\"}"},
        },
        ["document_id", "element_id", "parameters"],
    )
    def encode_configuration(args: Dict[str, Any]) -> Dict[str, Any]:
        body = {"parameters": [{"parameterId": k, "parameterValue": str(v)} for k, v in args["parameters"].items()]}
        result = client().request(
            "POST", f"/elements/d/{args['document_id']}/e/{args['element_id']}/configurationencodings", body=body
        ) or {}
        query_param = result.get("queryParam", "")
        return {
            "encoded_id": result.get("encodedId"),
            "query_param": query_param,
            "configuration": query_param.split("=", 1)[1] if "=" in query_param else query_param,
        }

    @server.tool(
        "onshape_eval_featurescript",
        "Evaluate a FeatureScript lambda against a Part Studio, e.g. a tight bounding box with evBox3d. "
        "Evaluation does not change the model. Onshape only evaluates lambda expressions here." + UNVERIFIED,
        {"document_id": ID, "wvm": WVM, "wvm_id": ID, "element_id": ID, "script": {"type": "string", "minLength": 1}},
        ["document_id", "wvm", "wvm_id", "element_id", "script"],
    )
    def eval_featurescript(args: Dict[str, Any]) -> Any:
        return client().request(
            "POST", "/partstudios" + wvm_path(args) + "/featurescript", body={"script": args["script"], "queries": []}
        )

    @server.tool(
        "onshape_copy_workspace",
        "Copy a workspace into a NEW document and return the new document's ids. This is how a variant or an "
        "adapted prior model starts: the source document is read, never written." + UNVERIFIED,
        {"document_id": ID, "workspace_id": ID, "new_name": {"type": "string", "minLength": 1}},
        ["document_id", "workspace_id", "new_name"],
        read_only=False,
    )
    def copy_workspace(args: Dict[str, Any]) -> Dict[str, Any]:
        result = client().request(
            "POST",
            f"/documents/{args['document_id']}/workspaces/{args['workspace_id']}/copy",
            body={"newName": args["new_name"], "isPublic": False},
        ) or {}
        new_did = result.get("newDocumentId")
        new_wid = result.get("newWorkspaceId")
        if new_did and new_did == args["document_id"]:
            raise ToolError("Onshape returned the source document id for the copy. Stopping: nothing will be written.")
        return {"new_document_id": new_did, "new_workspace_id": new_wid, "name": args["new_name"]}

    @server.tool(
        "onshape_set_variables",
        "Set variable expressions in a COPIED document: Part Studio #variable features, or a Variable Studio's "
        "variables. Refuses a protected base document, and refuses unless the document's name matches "
        "expected_document_name. Returns each variable from -> to; read health and properties afterwards." + UNVERIFIED,
        {
            "document_id": ID,
            "workspace_id": ID,
            "element_id": ID,
            "element_kind": {"type": "string", "enum": ["PARTSTUDIO", "VARIABLESTUDIO"]},
            "expected_document_name": {"type": "string", "minLength": 1},
            "assignments": {"type": "object", "description": "variable name -> expression with units, e.g. {\"wall\": \"3.5 mm\"}"},
        },
        ["document_id", "workspace_id", "element_id", "element_kind", "expected_document_name", "assignments"],
        read_only=False,
    )
    def set_variables(args: Dict[str, Any]) -> Dict[str, Any]:
        did, wid, eid = args["document_id"], args["workspace_id"], args["element_id"]
        protected.check_writable(did)
        document = client().request("GET", f"/documents/{did}") or {}
        if document.get("name") != args["expected_document_name"]:
            raise ToolError(
                f"Refused: document {did} is named {document.get('name')!r}, not {args['expected_document_name']!r}. "
                "Nothing was written."
            )
        assignments: Dict[str, str] = {k: str(v) for k, v in args["assignments"].items()}
        changes = []
        if args["element_kind"] == "VARIABLESTUDIO":
            tables = client().request("GET", f"/variables/d/{did}/w/{wid}/e/{eid}/variables") or []
            variables = [v for t in tables for v in (t.get("variables") or [])]
            known = {v.get("name") for v in variables}
            missing = sorted(set(assignments) - known)
            if missing:
                raise ToolError(f"Not in this Variable Studio: {', '.join(missing)}. Nothing was written.")
            body = []
            for variable in variables:
                entry = {k: variable.get(k) for k in ("name", "type", "expression", "description") if variable.get(k) is not None}
                if variable.get("name") in assignments:
                    changes.append({"name": variable["name"], "from": variable.get("expression"), "to": assignments[variable["name"]]})
                    entry["expression"] = assignments[variable["name"]]
                body.append(entry)
            client().request("POST", f"/variables/d/{did}/w/{wid}/e/{eid}/variables", body=body)
        else:
            response = client().request("GET", f"/partstudios/d/{did}/w/{wid}/e/{eid}/features") or {}
            features = response.get("features", []) or []
            by_name = {}
            for feature in features:
                if feature.get("featureType") == "assignVariable":
                    by_name[(parameter_value(feature, "name") or {}).get("value")] = feature
            missing = sorted(set(assignments) - set(by_name))
            if missing:
                raise ToolError(f"No #variable feature named: {', '.join(missing)}. Nothing was written.")
            for name, expression in assignments.items():
                feature = json.loads(json.dumps(by_name[name]))
                target = None
                for parameter_id in VARIABLE_VALUE_PARAMETER_IDS:
                    target = parameter_value(feature, parameter_id)
                    if target and "expression" in target:
                        break
                if not target:
                    raise ToolError(f"#variable {name} has no value parameter this connector recognises. Stopped before writing it.")
                changes.append({"name": name, "from": target.get("expression"), "to": expression})
                target["expression"] = expression
                client().request(
                    "POST",
                    f"/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{feature['featureId']}",
                    body={
                        "btType": "BTFeatureDefinitionCall-1406",
                        "feature": feature,
                        "serializationVersion": response.get("serializationVersion"),
                        "sourceMicroversion": response.get("sourceMicroversion"),
                    },
                )
        return {"document_id": did, "changes": changes, "next": "Read health and properties before recording this variant."}

    @server.tool(
        "onshape_create_version",
        "Create a named version (an immutable snapshot) of a workspace. Use it to pin a variant's result, or to "
        "record the base at the start of a run. A version changes no geometry." + UNVERIFIED,
        {"document_id": ID, "workspace_id": ID, "name": {"type": "string", "minLength": 1}},
        ["document_id", "workspace_id", "name"],
        read_only=False,
    )
    def create_version(args: Dict[str, Any]) -> Dict[str, Any]:
        result = client().request(
            "POST",
            f"/documents/d/{args['document_id']}/versions",
            body={"documentId": args["document_id"], "workspaceId": args["workspace_id"], "name": args["name"]},
        ) or {}
        return {"version_id": result.get("id"), "name": result.get("name"), "microversion": result.get("microversion")}

    return server


def client_from_environment() -> OnshapeClient:
    return OnshapeClient(
        access_key=os.environ.get("ONSHAPE_ACCESS_KEY", ""),
        secret_key=os.environ.get("ONSHAPE_SECRET_KEY", ""),
        base_url=os.environ.get("ONSHAPE_BASE_URL", "https://cad.onshape.com"),
        api_version=os.environ.get("ONSHAPE_API_VERSION", "v10"),
    )


def main() -> None:
    state_path = Path(
        os.environ.get("ONSHAPE_BRIDGE_STATE", str(Path.home() / ".manufacturing-onshape" / "protected.json"))
    )
    build_server(client_from_environment, ProtectedDocuments(state_path)).serve()


if __name__ == "__main__":
    main()
