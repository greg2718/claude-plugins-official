#!/usr/bin/env python3
"""Onshape bridge: the CAD adapter surface of manufacturing-onshape, over MCP stdio.

Implements the operations `skills/design-space-explore/reference/cad-adapter.md`
lists (variants, open-apply-save-as-new, building a part from a drawing, STEP
import, and the integrity check), against Onshape's public REST API. Standard
library only. It writes only to documents it made: copies, new documents and
imported documents. CAM is not implemented: Onshape publishes no CAM Studio API.

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

VERSION = "0.3.0"
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

    def request(
        self,
        method: str,
        endpoint: str,
        query: Optional[Dict[str, Any]] = None,
        body: Any = None,
        raw: Optional[Tuple[bytes, str]] = None,
    ) -> Any:
        """`body` is sent as JSON; `raw` is (bytes, content type), for a multipart upload."""
        path = f"/api/{self.api_version}{endpoint}"
        query_string = urllib.parse.urlencode(query or {}, doseq=True)
        if raw is not None:
            data, content_type = raw
        else:
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


KNOWN_STATUSES = ("OK", "INFO", "WARNING", "ERROR", "UNKNOWN")


def feature_health(features_response: Any) -> Dict[str, Any]:
    """Split the feature states into failed, warning and ok.

    `featureStatus` takes OK, INFO, WARNING, ERROR or UNKNOWN (GBTNodeStatusType).
    ERROR and UNKNOWN fail a variant; WARNING is reported and does not.

    `regenerated` is True only on positive evidence: every active feature listed
    in the response has a state, and every state is OK, INFO or WARNING. A
    missing response, missing states, a feature with no state, or a status the
    bridge does not recognise makes it None (unknown), which blocks measuring
    exactly as a failure does.
    """
    if not isinstance(features_response, dict):
        return {"regenerated": None, "failed_features": [], "warning_features": [], "unknown_features": [],
                "feature_count": 0, "note": "Onshape returned no feature list, so regeneration health is unknown."}
    features = [f for f in (features_response.get("features") or []) if isinstance(f, dict)]
    names = {f.get("featureId"): f.get("name") for f in features}
    states = features_response.get("featureStates")
    failed, warnings, unknown = [], [], []
    notes = []
    if not isinstance(states, dict) or not states:
        notes.append("Onshape returned no feature states")
        states = states if isinstance(states, dict) else {}
    for feature_id, state in states.items():
        state = state if isinstance(state, dict) else {}
        if state.get("inactive"):
            continue
        status = state.get("featureStatus")
        entry = {"feature_id": feature_id, "feature_name": names.get(feature_id), "status": status}
        if status in ("ERROR", "UNKNOWN"):
            failed.append(entry)
        elif status == "WARNING":
            warnings.append(entry)
        elif status not in ("OK", "INFO"):
            unknown.append(entry)
    for feature in features:
        if feature.get("featureId") not in states and not feature.get("suppressed"):
            unknown.append({"feature_id": feature.get("featureId"), "feature_name": feature.get("name"), "status": None})
    if unknown:
        notes.append(f"{len(unknown)} feature(s) have a missing or unrecognised status")
    if failed:
        regenerated: Optional[bool] = False
    elif notes:
        regenerated = None
    else:
        regenerated = True
    health: Dict[str, Any] = {
        "regenerated": regenerated,
        "failed_features": failed,
        "warning_features": warnings,
        "unknown_features": unknown,
        "feature_count": len(states),
    }
    if regenerated is None:
        health["note"] = "; ".join(notes) + ". Health is unknown: do not measure or record this variant as regenerated."
    return health


def feature_inventory(features_response: Any) -> List[Dict[str, Any]]:
    """The Part Studio's features in order, for the before/after integrity comparison."""
    features = (features_response or {}).get("features") if isinstance(features_response, dict) else None
    return [
        {"feature_id": f.get("featureId"), "name": f.get("name"), "type": f.get("featureType"), "suppressed": bool(f.get("suppressed"))}
        for f in (features or [])
        if isinstance(f, dict)
    ]


INTEGRITY_NOTE = (
    "Use onshape_list_contents before and after a step for the integrity check: it lists features (sketches are "
    "the features of type newSketch), bodies, and the external references the feature list shows. Record the "
    "check as clean only when both reads succeeded and the after-list holds exactly the before-list plus what "
    "the step made."
)

# ---------------------------------------------------------------------------
# Building features (unit-tested). Lengths come in millimetres and go to Onshape in metres
# (sketch geometry) or as expressions with units (feature parameters).
# ---------------------------------------------------------------------------

PLANES = ("Top", "Front", "Right")
EXTRUDE_OPERATIONS = ("NEW", "ADD", "REMOVE")
END_BOUNDS = ("BLIND", "THROUGH_ALL")
GOOD_STATUSES = ("OK", "INFO", "WARNING")
MAX_IMPORT_BYTES = 100 * 1024 * 1024


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise ToolError(f"{label} must be a finite number in millimetres, not {value!r}. Nothing was written.")
    return float(value)


def _positive(value: Any, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ToolError(f"{label} must be greater than zero, not {value!r}. Nothing was written.")
    return number


def _feature_name(spec: Dict[str, Any], default: str) -> str:
    name = spec.get("name", default)
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ToolError("name must be a non-empty string of at most 100 characters. Nothing was written.")
    return name


def _line_entity(entity_id: str, x1: float, y1: float, x2: float, y2: float) -> Dict[str, Any]:
    """A sketch line segment in metres (BTMSketchCurveSegment-155 over BTCurveGeometryLine-117)."""
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length <= 0:
        raise ToolError(f"Sketch line {entity_id} has zero length. Nothing was written.")
    return {
        "btType": "BTMSketchCurveSegment-155",
        "entityId": entity_id,
        "startPointId": f"{entity_id}.start",
        "endPointId": f"{entity_id}.end",
        "startParam": 0.0,
        "endParam": length,
        "geometry": {
            "btType": "BTCurveGeometryLine-117",
            "pntX": x1,
            "pntY": y1,
            "dirX": (x2 - x1) / length,
            "dirY": (y2 - y1) / length,
        },
    }


def sketch_entities(entities: Any) -> List[Dict[str, Any]]:
    """Rectangles, circles and lines (millimetres, in the sketch plane's x and y) as Onshape sketch entities."""
    if not isinstance(entities, list) or not entities:
        raise ToolError("A sketch needs at least one entity (rectangle, circle or line). Nothing was written.")
    out: List[Dict[str, Any]] = []
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            raise ToolError(f"Sketch entity {index} is not an object. Nothing was written.")
        kind = entity.get("type")
        tag = f"e{index}"
        mm = 0.001
        if kind == "rectangle":
            x1, y1 = _finite(entity.get("x1"), f"entity {index} x1"), _finite(entity.get("y1"), f"entity {index} y1")
            x2, y2 = _finite(entity.get("x2"), f"entity {index} x2"), _finite(entity.get("y2"), f"entity {index} y2")
            if x1 == x2 or y1 == y2:
                raise ToolError(f"Rectangle {index} has zero width or height. Nothing was written.")
            corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            for side in range(4):
                (ax, ay), (bx, by) = corners[side], corners[(side + 1) % 4]
                out.append(_line_entity(f"{tag}.side{side}", ax * mm, ay * mm, bx * mm, by * mm))
        elif kind == "circle":
            cx, cy = _finite(entity.get("cx"), f"entity {index} cx"), _finite(entity.get("cy"), f"entity {index} cy")
            radius = _positive(entity.get("radius"), f"entity {index} radius")
            out.append(
                {
                    "btType": "BTMSketchCurve-4",
                    "entityId": tag,
                    "centerId": f"{tag}.center",
                    "geometry": {
                        "btType": "BTCurveGeometryCircle-115",
                        "radius": radius * mm,
                        "xCenter": cx * mm,
                        "yCenter": cy * mm,
                        "xDir": 1,
                        "yDir": 0,
                        "clockwise": False,
                    },
                }
            )
        elif kind == "line":
            values = [_finite(entity.get(k), f"entity {index} {k}") for k in ("x1", "y1", "x2", "y2")]
            out.append(_line_entity(tag, *(v * mm for v in values)))
        else:
            raise ToolError(f"Sketch entity {index} has type {kind!r}; use rectangle, circle or line. Nothing was written.")
    return out


def sketch_feature(spec: Dict[str, Any]) -> Dict[str, Any]:
    plane = spec.get("plane")
    if plane not in PLANES:
        raise ToolError(f"plane must be one of {', '.join(PLANES)}. Nothing was written.")
    return {
        "btType": "BTMSketch-151",
        "featureType": "newSketch",
        "name": _feature_name(spec, "Sketch"),
        "parameters": [
            {
                "btType": "BTMParameterQueryList-148",
                "parameterId": "sketchPlane",
                "queries": [
                    {"btType": "BTMIndividualQuery-138", "queryString": f'query=qCreatedBy(makeId("{plane}"), EntityType.FACE);'}
                ],
            }
        ],
        "entities": sketch_entities(spec.get("entities")),
        "constraints": [],
    }


def _enum(parameter_id: str, value: str, enum_name: str) -> Dict[str, Any]:
    return {"btType": "BTMParameterEnum-145", "parameterId": parameter_id, "value": value, "enumName": enum_name}


def _quantity(parameter_id: str, millimetres: float) -> Dict[str, Any]:
    return {"btType": "BTMParameterQuantity-147", "parameterId": parameter_id, "expression": f"{millimetres:g} mm"}


def extrude_feature(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Extrude a sketch's regions: a new body, added to bodies, or removed from them (a cut or a through hole)."""
    sketch_id = spec.get("sketch_feature_id")
    if not isinstance(sketch_id, str) or not sketch_id:
        raise ToolError("sketch_feature_id must name the sketch to extrude. Nothing was written.")
    operation = spec.get("operation", "NEW")
    if operation not in EXTRUDE_OPERATIONS:
        raise ToolError(f"operation must be one of {', '.join(EXTRUDE_OPERATIONS)}. Nothing was written.")
    end = spec.get("end", "BLIND")
    if end not in END_BOUNDS:
        raise ToolError(f"end must be one of {', '.join(END_BOUNDS)}. Nothing was written.")
    parameters = [
        _enum("bodyType", "SOLID", "ExtendedToolBodyType"),
        _enum("operationType", operation, "NewBodyOperationType"),
        {
            "btType": "BTMParameterQueryList-148",
            "parameterId": "entities",
            "queries": [{"btType": "BTMIndividualSketchRegionQuery-140", "featureId": sketch_id}],
        },
        _enum("endBound", end, "BoundingType"),
    ]
    if end == "BLIND":
        parameters.append(_quantity("depth", _positive(spec.get("depth_mm"), "depth_mm")))
    if spec.get("opposite_direction"):
        parameters.append({"btType": "BTMParameterBoolean-144", "parameterId": "oppositeDirection", "value": True})
    return {
        "btType": "BTMFeature-134",
        "featureType": "extrude",
        "name": _feature_name(spec, "Extrude"),
        "parameters": parameters,
        "returnAfterSubfeatures": False,
        "suppressed": False,
    }


def edge_points(spec: Dict[str, Any]) -> List[Tuple[float, float, float]]:
    points = spec.get("edge_points_mm")
    if not isinstance(points, list) or not points:
        raise ToolError("edge_points_mm must list one point on each edge, as [x, y, z] in millimetres. Nothing was written.")
    out = []
    for index, point in enumerate(points):
        if not isinstance(point, list) or len(point) != 3:
            raise ToolError(f"Edge point {index} must be [x, y, z] in millimetres. Nothing was written.")
        out.append(tuple(_finite(v, f"edge point {index}") for v in point))  # type: ignore[misc]
    return out  # type: ignore[return-value]


def edge_at(point: Tuple[float, float, float]) -> str:
    """FeatureScript query for the solid-body edges through one point (millimetres)."""
    x, y, z = point
    return f"qContainsPoint(qBodyType(qEverything(EntityType.EDGE), BodyType.SOLID), vector({x:g}, {y:g}, {z:g}) * millimeter)"


def edges_query(points: List[Tuple[float, float, float]]) -> Dict[str, Any]:
    return {
        "btType": "BTMParameterQueryList-148",
        "parameterId": "entities",
        "queries": [{"btType": "BTMIndividualQuery-138", "queryString": "query=qUnion([" + ", ".join(edge_at(p) for p in points) + "]);"}],
    }


def fillet_feature(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "btType": "BTMFeature-134",
        "featureType": "fillet",
        "name": _feature_name(spec, "Fillet"),
        "parameters": [edges_query(edge_points(spec)), _quantity("radius", _positive(spec.get("radius_mm"), "radius_mm"))],
        "returnAfterSubfeatures": False,
        "suppressed": False,
    }


def chamfer_feature(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "btType": "BTMFeature-134",
        "featureType": "chamfer",
        "name": _feature_name(spec, "Chamfer"),
        "parameters": [
            edges_query(edge_points(spec)),
            _enum("chamferType", "EQUAL_OFFSETS", "ChamferType"),
            _quantity("width", _positive(spec.get("distance_mm"), "distance_mm")),
        ],
        "returnAfterSubfeatures": False,
        "suppressed": False,
    }


FEATURE_BUILDERS = {"sketch": sketch_feature, "extrude": extrude_feature, "fillet": fillet_feature, "chamfer": chamfer_feature}


def count_script(query: str) -> str:
    return f"function(context is Context, queries) {{ return size(evaluateQuery(context, {query})); }}"


def fs_number(answer: Any) -> Optional[float]:
    """The number a FeatureScript evaluation returned, or None when the answer is not one."""
    result = answer.get("result") if isinstance(answer, dict) else None
    if isinstance(result, dict):
        value = result.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def summarize_bodies(details: Any, with_faces: bool = False) -> Optional[List[Dict[str, Any]]]:
    """Bodies from /bodydetails, or None when the answer has no body list (unknown, never empty)."""
    bodies = details.get("bodies") if isinstance(details, dict) else None
    if not isinstance(bodies, list):
        return None
    out = []
    for body in bodies:
        if not isinstance(body, dict):
            continue
        properties = body.get("properties") if isinstance(body.get("properties"), dict) else {}
        faces = [f for f in (body.get("faces") or []) if isinstance(f, dict)]
        entry: Dict[str, Any] = {
            "body_id": body.get("id"),
            "type": body.get("type"),
            "name": properties.get("name") or body.get("name"),
            "face_count": len(faces),
        }
        if with_faces:
            entry["faces"] = [summarize_face(f) for f in faces]
        out.append(entry)
    return out


def summarize_face(face: Dict[str, Any]) -> Dict[str, Any]:
    surface = face.get("surface") if isinstance(face.get("surface"), dict) else {}
    kind = surface.get("type")
    entry: Dict[str, Any] = {"face_id": face.get("id"), "surface": kind, "area_m2": face.get("area")}
    if kind == "PLANE":
        entry["normal"] = surface.get("normal")
        entry["origin_m"] = surface.get("origin")
    elif kind == "CYLINDER":
        entry["radius_m"] = surface.get("radius")
        entry["axis"] = surface.get("axis")
        entry["origin_m"] = surface.get("origin")
    return entry


def external_references(features_response: Any) -> Dict[str, Any]:
    """References to other documents or tabs, as the feature list shows them: derived features
    (importDerived) and parameters of a BTMParameterReference type. `present` is None when there
    is no feature list to scan."""
    features = features_response.get("features") if isinstance(features_response, dict) else None
    if not isinstance(features, list):
        return {"present": None, "found": [], "basis": "Onshape returned no feature list, so references are unknown."}
    found = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        label = feature.get("name") or feature.get("featureId")
        if feature.get("featureType") == "importDerived":
            found.append({"feature": label, "kind": "derived"})
            continue
        if '"BTMParameterReference' in json.dumps(feature.get("parameters", [])):
            found.append({"feature": label, "kind": "reference parameter"})
    return {
        "present": bool(found),
        "found": found,
        "basis": "Scanned this Part Studio's feature list for derived features and reference parameters. References "
        "held in other tabs, or in FeatureScript code, are not seen.",
    }


def contents_snapshot(features_response: Any, details: Any) -> Dict[str, Any]:
    inventory = feature_inventory(features_response) if isinstance(features_response, dict) else None
    return {
        "features": inventory,
        "sketches": [f["name"] for f in inventory if f["type"] == "newSketch"] if inventory is not None else None,
        "bodies": summarize_bodies(details),
    }


def integrity_compare(before: Dict[str, Any], after: Dict[str, Any], made: List[str]) -> Dict[str, Any]:
    """`clean` only when both snapshots were read and after holds exactly before's features plus `made`
    (feature ids). Bodies may change in count (an extrude makes one), so they are recorded, not compared,
    except that a read with no body list makes the result not_checked."""
    if before.get("features") is None or after.get("features") is None or before.get("bodies") is None or after.get("bodies") is None:
        return {"result": "not_checked", "detail": "A feature or body list could not be read.", "before": before, "after": after}
    before_ids = [f["feature_id"] for f in before["features"]]
    after_ids = [f["feature_id"] for f in after["features"]]
    expected = before_ids + list(made)
    if after_ids == expected:
        return {"result": "clean", "before": before, "after": after}
    unexpected = [i for i in after_ids if i not in expected]
    missing = [i for i in expected if i not in after_ids]
    return {
        "result": "foreign",
        "unexpected_features": unexpected,
        "missing_features": missing,
        "detail": "The feature list after the step is not the list before plus what this step made.",
        "before": before,
        "after": after,
    }


def multipart_body(fields: Dict[str, str], file_field: str, file_name: str, content: bytes) -> Tuple[bytes, str]:
    boundary = "----onshape-bridge-" + secrets.token_hex(12)
    parts: List[bytes] = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    safe_name = re.sub(r'[^A-Za-z0-9._-]', "_", file_name)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


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


STATE_FORMAT = 2


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


class ProtectedDocuments:
    """What the bridge may and may not write, persisted so a restarted bridge keeps refusing.

    `entries`: base documents read at the start of a run. Never written.
    `copies`: documents this bridge made with onshape_copy_workspace, with the one
    workspace it made. The only documents it writes.

    If the state file exists but cannot be read, nothing is written at all until a
    person fixes or removes it (fail closed).
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
            elif "format" not in data and all(isinstance(v, dict) and "workspace_id" in v for v in data.values()):
                protected, copies = data, {}  # the first version's file: protected bases only
            else:
                raise ValueError("unrecognised layout")
            for record in list(protected.values()) + list(copies.values()):
                if not isinstance(record, dict) or not record.get("workspace_id"):
                    raise ValueError("an entry has no workspace id")
            self.entries, self.copies = protected, copies
        except (OSError, ValueError) as err:
            self.error = f"The bridge's state file {path} cannot be read ({err})."
            log(self.error + " Refusing every write until it is fixed or removed.")

    def require_readable(self) -> None:
        if self.error:
            raise ToolError(
                self.error + " It records which base documents are protected, so the bridge writes nothing until "
                "a person checks it, then fixes or removes it. Nothing was changed."
            )

    def _save(self) -> None:
        write_json_atomically(self.path, {"format": STATE_FORMAT, "protected": self.entries, "copies": self.copies})

    def add(self, document_id: str, record: Dict[str, Any]) -> None:
        self.require_readable()
        if document_id in self.copies:
            raise ToolError(f"Document {document_id} is a copy this bridge made, not a base model.")
        self.entries[document_id] = record
        self._save()

    def register_copy(self, document_id: str, record: Dict[str, Any]) -> None:
        self.require_readable()
        if document_id in self.entries:
            raise ToolError(f"Refused: document {document_id} is a protected base model.")
        self.copies[document_id] = record
        self._save()

    def retire_copy(self, document_id: str, reason: str) -> None:
        record = self.copies.get(document_id)
        if record is not None:
            record["retired"] = reason
            try:
                self._save()
            except (OSError, ToolError) as err:
                log(f"Could not record the retired copy: {err}")

    def check_writable(self, document_id: str, workspace_id: Optional[str] = None) -> Dict[str, Any]:
        self.require_readable()
        if document_id in self.entries:
            name = self.entries[document_id].get("name")
            raise ToolError(
                f"Refused: document {document_id} ({name}) is a protected base model. Variants and estimating "
                "models are written to a copy, never the base. Copy the workspace to a new document first."
            )
        record = self.copies.get(document_id)
        if record is None:
            raise ToolError(
                f"Refused: document {document_id} is not a copy this bridge made. The bridge writes only to a "
                "document made with onshape_copy_workspace, never to one it did not create. Nothing was written."
            )
        if workspace_id is not None and workspace_id != record.get("workspace_id"):
            raise ToolError(
                f"Refused: workspace {workspace_id} is not the workspace the bridge made in copy {document_id}. "
                "Nothing was written."
            )
        if record.get("retired"):
            raise ToolError(
                f"Refused: copy {document_id} was left in an unknown state ({record['retired']}). Discard it and "
                "make a new copy. Nothing was written."
            )
        return record


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
            "Every write goes to a document made with onshape_copy_workspace; the bridge refuses any other."
        ),
    )
    state: Dict[str, Optional[OnshapeClient]] = {"client": None}

    def client() -> OnshapeClient:
        if state["client"] is None:
            state["client"] = client_factory()
        return state["client"]

    def current_microversion(did: str, wid: str) -> Optional[str]:
        answer = client().request("GET", f"/documents/d/{did}/w/{wid}/currentmicroversion")
        value = answer.get("microversion") if isinstance(answer, dict) else None
        return value if isinstance(value, str) and value.strip() else None

    def protect_document(did: str, wid: str) -> Dict[str, Any]:
        document = client().request("GET", f"/documents/{did}") or {}
        microversion = current_microversion(did, wid)
        if not microversion:
            raise ToolError(
                f"Onshape returned no microversion for document {did}, workspace {wid}, so the bridge could not record "
                "where the base stands. It was not protected, and nothing will be copied from it."
            )
        record = {
            "name": document.get("name"),
            "workspace_id": wid,
            "microversion": microversion,
            "protected_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        protected.add(did, record)
        return record

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
            "bridge_copies": len(protected.copies),
            "state_error": protected.error,
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
            "bridge_copy": did in protected.copies,
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
        protected.require_readable()
        record = protect_document(did, wid)
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
        recorded = record.get("microversion")
        if not isinstance(recorded, str) or not recorded.strip():
            raise ToolError(
                f"No microversion was recorded when document {did} was protected, so the bridge cannot say whether it "
                "moved. Treat the base as unchecked and tell the user."
            )
        current = current_microversion(did, record["workspace_id"])
        if not current:
            raise ToolError(
                f"Onshape returned no current microversion for document {did}, so the bridge cannot say whether it "
                "moved. Treat the base as unchecked and tell the user."
            )
        return {
            "document_id": did,
            "recorded_microversion": recorded,
            "current_microversion": current,
            "untouched": current == recorded,
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
            "features": feature_inventory(features_response),
            "integrity_note": INTEGRITY_NOTE,
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
        if health["regenerated"] is not True:
            reason = "Failed regeneration" if health["regenerated"] is False else "Regeneration health unknown"
            return {"health": health, "properties": None, "note": f"{reason}: not measured."}
        mass = client().request("GET", base + "/massproperties", query)
        boxes = client().request("GET", base + "/boundingboxes", query)
        return {
            "health": health,
            "mass_properties": summarize_mass(mass),
            "bounding_box_m": boxes,
            "configuration": args.get("configuration"),
            "bounding_box_note": "Onshape documents these boxes as approximate. Use onshape_eval_featurescript with evBox3d tight, and the same configuration, for a check against a hand calculation.",
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
        "Evaluation does not change the model. Onshape only evaluates lambda expressions here. Pass the same "
        "configuration as the property read it checks, or the default configuration is evaluated." + UNVERIFIED,
        {
            "document_id": ID,
            "wvm": WVM,
            "wvm_id": ID,
            "element_id": ID,
            "script": {"type": "string", "minLength": 1},
            "configuration": {"type": "string", "description": "Encoded configuration, as for onshape_read_properties"},
        },
        ["document_id", "wvm", "wvm_id", "element_id", "script"],
    )
    def eval_featurescript(args: Dict[str, Any]) -> Any:
        query = {"configuration": args["configuration"]} if args.get("configuration") else None
        result = client().request(
            "POST", "/partstudios" + wvm_path(args) + "/featurescript", query, body={"script": args["script"], "queries": []}
        )
        return {"configuration": args.get("configuration") or "default", "result": result}

    @server.tool(
        "onshape_copy_workspace",
        "Copy a workspace into a NEW document and return the new document's ids. This is how a variant or an "
        "adapted prior model starts: the source document is read, never written. If the source is not yet "
        "protected, it is protected first. Registers the copy as the only kind of document the bridge writes." + UNVERIFIED,
        {"document_id": ID, "workspace_id": ID, "new_name": {"type": "string", "minLength": 1}},
        ["document_id", "workspace_id", "new_name"],
        read_only=False,
    )
    def copy_workspace(args: Dict[str, Any]) -> Dict[str, Any]:
        protected.require_readable()
        source = args["document_id"]
        if source in protected.copies:
            raise ToolError(
                "Refused: this document is already a copy this bridge made. Make each copy from the base model, so "
                "every variant starts from the same geometry."
            )
        protected_now = False
        if source not in protected.entries:
            # The base is protected here, by the bridge, rather than trusting that onshape_protect_base ran first.
            protect_document(source, args["workspace_id"])
            protected_now = True
        result = client().request(
            "POST",
            f"/documents/{args['document_id']}/workspaces/{args['workspace_id']}/copy",
            body={"newName": args["new_name"], "isPublic": False},
        ) or {}
        new_did = result.get("newDocumentId")
        new_wid = result.get("newWorkspaceId")
        if not new_did or not new_wid:
            raise ToolError("Onshape did not return the new document and workspace ids. Nothing will be written to any copy.")
        if new_did == source:
            raise ToolError("Onshape returned the source document id for the copy. Stopping: nothing will be written.")
        protected.register_copy(
            new_did,
            {
                "workspace_id": new_wid,
                "name": args["new_name"],
                "source_document_id": source,
                "source_workspace_id": args["workspace_id"],
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
        )
        return {"new_document_id": new_did, "new_workspace_id": new_wid, "name": args["new_name"], "source_protected_by_this_call": protected_now}

    @server.tool(
        "onshape_set_variables",
        "Set variable expressions in a document this bridge copied: Part Studio #variable features, or a Variable "
        "Studio's variables. Writes only to a registered copy and its own workspace, and only when the document's "
        "name matches expected_document_name. Checks every assignment before writing any, and undoes earlier writes "
        "if a later one fails. Returns a change journal (from -> to, written, undone) and, for a Part Studio, the "
        "regeneration health read after the writes." + UNVERIFIED,
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
        protected.check_writable(did, wid)
        document = client().request("GET", f"/documents/{did}") or {}
        if document.get("name") != args["expected_document_name"]:
            raise ToolError(
                f"Refused: document {did} is named {document.get('name')!r}, not {args['expected_document_name']!r}. "
                "Nothing was written."
            )
        if not isinstance(args["assignments"], dict) or not args["assignments"]:
            raise ToolError("assignments must name at least one variable. Nothing was written.")
        assignments: Dict[str, str] = {k: str(v) for k, v in args["assignments"].items()}
        blank = sorted(k for k, v in assignments.items() if not v.strip())
        if blank:
            raise ToolError(f"No expression given for: {', '.join(blank)}. Nothing was written.")
        changes: List[Dict[str, Any]] = []
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
                    changes.append({"name": variable["name"], "from": variable.get("expression"), "to": assignments[variable["name"]], "written": False})
                    entry["expression"] = assignments[variable["name"]]
                body.append(entry)
            client().request("POST", f"/variables/d/{did}/w/{wid}/e/{eid}/variables", body=body)  # one call: all or none
            for change in changes:
                change["written"] = True
            return {
                "document_id": did,
                "changes": changes,
                "health": {"regenerated": None, "note": "A Variable Studio has no geometry. Read the Part Studio's health with onshape_read_properties."},
            }

        # Part Studio #variable features: check and prepare every update before writing any.
        response = client().request("GET", f"/partstudios/d/{did}/w/{wid}/e/{eid}/features") or {}
        features = response.get("features", []) or []
        by_name = {}
        for feature in features:
            if feature.get("featureType") == "assignVariable":
                by_name[(parameter_value(feature, "name") or {}).get("value")] = feature
        missing = sorted(set(assignments) - set(by_name))
        if missing:
            raise ToolError(f"No #variable feature named: {', '.join(missing)}. Nothing was written.")
        prepared = []
        unrecognised = []
        for name, expression in assignments.items():
            original = by_name[name]
            feature = json.loads(json.dumps(original))
            target = None
            for parameter_id in VARIABLE_VALUE_PARAMETER_IDS:
                target = parameter_value(feature, parameter_id)
                if target and "expression" in target:
                    break
            if not target or "expression" not in target:
                unrecognised.append(name)
                continue
            before = target.get("expression")
            target["expression"] = expression
            prepared.append({"name": name, "feature": feature, "original": original, "from": before, "to": expression})
        if unrecognised:
            raise ToolError(f"No value parameter this connector recognises on: {', '.join(unrecognised)}. Nothing was written.")

        def post(feature: Dict[str, Any], source_microversion: Any) -> Any:
            return client().request(
                "POST",
                f"/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{feature['featureId']}",
                body={
                    "btType": "BTFeatureDefinitionCall-1406",
                    "feature": feature,
                    "serializationVersion": response.get("serializationVersion"),
                    "sourceMicroversion": source_microversion,
                },
            )

        source_microversion = response.get("sourceMicroversion")
        written: List[Dict[str, Any]] = []
        for item in prepared:
            change = {"name": item["name"], "from": item["from"], "to": item["to"], "written": False}
            changes.append(change)
            try:
                answer = post(item["feature"], source_microversion)
            except ToolError as err:
                # Undo what this call wrote, newest first, so the copy is as it was before the call.
                undo_failed = []
                for done in reversed(written):
                    try:
                        latest = client().request("GET", f"/partstudios/d/{did}/w/{wid}/e/{eid}/features") or {}
                        post(done["original"], latest.get("sourceMicroversion"))
                        done["change"]["undone"] = True
                    except ToolError as undo_err:
                        done["change"]["undone"] = False
                        undo_failed.append(f"{done['name']}: {undo_err}")
                journal = json.dumps(changes)
                if undo_failed:
                    protected.retire_copy(did, "a variable change failed and could not be fully undone")
                    raise ToolError(
                        f"Writing {item['name']} failed: {err}. Undoing the earlier changes did not fully succeed "
                        f"({'; '.join(undo_failed)}), so this copy is in an unknown state and the bridge will not write "
                        f"to it again. Discard it and make a new copy. Change journal: {journal}"
                    ) from err
                raise ToolError(
                    f"Writing {item['name']} failed: {err}. The {len(written)} earlier change(s) in this call were undone. "
                    f"Change journal: {journal}"
                ) from err
            change["written"] = True
            if isinstance(answer, dict) and isinstance(answer.get("sourceMicroversion"), str):
                source_microversion = answer["sourceMicroversion"]
            written.append({"name": item["name"], "original": item["original"], "change": change})

        after = client().request("GET", f"/partstudios/d/{did}/w/{wid}/e/{eid}/features")
        return {
            "document_id": did,
            "changes": changes,
            "health": feature_health(after),
            "features": feature_inventory(after),
            "next": "Measure only if health.regenerated is true.",
        }

    @server.tool(
        "onshape_create_version",
        "Create a named version (an immutable snapshot) of a copied document's workspace, to pin a variant's "
        "result. A version changes no geometry. Only for a copy this bridge made." + UNVERIFIED,
        {"document_id": ID, "workspace_id": ID, "name": {"type": "string", "minLength": 1}},
        ["document_id", "workspace_id", "name"],
        read_only=False,
    )
    def create_version(args: Dict[str, Any]) -> Dict[str, Any]:
        protected.check_writable(args["document_id"], args["workspace_id"])
        result = client().request(
            "POST",
            f"/documents/d/{args['document_id']}/versions",
            body={"documentId": args["document_id"], "workspaceId": args["workspace_id"], "name": args["name"]},
        ) or {}
        return {"version_id": result.get("id"), "name": result.get("name"), "microversion": result.get("microversion")}

    # -------------------------------------------------------------- new documents, STEP import, building

    def require_name(did: str, expected: str) -> None:
        document = client().request("GET", f"/documents/{did}") or {}
        if document.get("name") != expected:
            raise ToolError(f"Refused: document {did} is named {document.get('name')!r}, not {expected!r}. Nothing was written.")

    def part_studios(did: str, wid: str) -> List[Dict[str, Any]]:
        elements = client().request("GET", f"/documents/d/{did}/w/{wid}/elements") or []
        return [e for e in elements if isinstance(e, dict) and e.get("elementType") == "PARTSTUDIO"]

    def snapshot(did: str, wvm: str, wvmid: str, eid: str) -> Tuple[Dict[str, Any], Any]:
        base = f"/partstudios/d/{did}/{wvm}/{wvmid}/e/{eid}"
        features_response = client().request("GET", base + "/features")
        try:
            details = client().request("GET", base + "/bodydetails")
        except ToolError as err:
            log(f"bodydetails failed: {err}")
            details = None
        return contents_snapshot(features_response, details), features_response

    def new_document(name: str, kind: str, extra: Dict[str, Any]) -> Tuple[str, str]:
        protected.require_readable()
        created = client().request("POST", "/documents", body={"name": name, "isPublic": False}) or {}
        did = created.get("id")
        wid = (created.get("defaultWorkspace") or {}).get("id")
        if not did or not wid:
            raise ToolError("Onshape did not return the new document's id and workspace. Nothing will be written to it.")
        if did in protected.entries or did in protected.copies:
            raise ToolError(f"Onshape returned document {did}, which the bridge already knows. Stopping: nothing will be written.")
        protected.register_copy(
            did, {"workspace_id": wid, "name": name, "kind": kind, "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), **extra}
        )
        return did, wid

    @server.tool(
        "onshape_create_document",
        "Create a NEW, private document for an estimating model built from a drawing or text (rfq-to-quote paths "
        "C2 and A), and register it as a document the bridge may write. Checks that its one Part Studio is empty. "
        "Name it <inquiry>_L<line>_<part>_R<rev>." + UNVERIFIED,
        {"name": {"type": "string", "minLength": 1}},
        ["name"],
        read_only=False,
    )
    def create_document(args: Dict[str, Any]) -> Dict[str, Any]:
        did, wid = new_document(args["name"], "created", {})
        studios = part_studios(did, wid)
        if len(studios) != 1:
            protected.retire_copy(did, f"a new document held {len(studios)} Part Studios, not one")
            raise ToolError(f"The new document {did} holds {len(studios)} Part Studios, not one. The bridge will not build in it.")
        eid = studios[0].get("id")
        contents, _ = snapshot(did, "w", wid, eid)
        if contents["features"] is None or contents["features"] or contents["bodies"] is None or contents["bodies"]:
            protected.retire_copy(did, "a new document was not empty, or its contents could not be read")
            raise ToolError(
                f"The new document {did} is not empty, or its contents could not be read: {json.dumps(contents)}. "
                "The bridge will not build in it."
            )
        protected.copies[did]["part_studio_id"] = eid
        protected._save()
        return {"document_id": did, "workspace_id": wid, "element_id": eid, "name": args["name"], "contents": contents}

    @server.tool(
        "onshape_list_contents",
        "List a Part Studio's features (sketches are type newSketch), bodies, and the external references its feature "
        "list shows. Read-only. Call before and after every build or adapt step: the integrity check." + UNVERIFIED,
        {"document_id": ID, "wvm": WVM, "wvm_id": ID, "element_id": ID},
        ["document_id", "wvm", "wvm_id", "element_id"],
    )
    def list_contents(args: Dict[str, Any]) -> Dict[str, Any]:
        contents, features_response = snapshot(args["document_id"], args["wvm"], args["wvm_id"], args["element_id"])
        return {**contents, "external_references": external_references(features_response), "integrity_note": INTEGRITY_NOTE}

    @server.tool(
        "onshape_read_faces",
        "Bodies and faces of a Part Studio: each face's surface type, area, and for planes the normal, for cylinders the "
        "radius and axis (metres). For an imported STEP model, holes and pockets are read from these faces: each one "
        "is a reading to confirm, not a feature. Read-only." + UNVERIFIED,
        {"document_id": ID, "wvm": WVM, "wvm_id": ID, "element_id": ID},
        ["document_id", "wvm", "wvm_id", "element_id"],
    )
    def read_faces(args: Dict[str, Any]) -> Dict[str, Any]:
        details = client().request("GET", "/partstudios" + wvm_path(args) + "/bodydetails")
        bodies = summarize_bodies(details, with_faces=True)
        if bodies is None:
            raise ToolError("Onshape returned no body list, so the faces could not be read.")
        return {"bodies": bodies, "units": "metres, square metres"}

    @server.tool(
        "onshape_add_feature",
        "Add one feature to a Part Studio in a document this bridge made or copied: kind sketch (spec: plane Top, "
        "Front or Right; entities of type rectangle {x1,y1,x2,y2}, circle {cx,cy,radius} or line {x1,y1,x2,y2}, in mm), "
        "extrude (spec: sketch_feature_id, operation NEW, ADD or REMOVE for a cut or through hole, end BLIND with "
        "depth_mm or THROUGH_ALL, opposite_direction), fillet (spec: edge_points_mm, one [x,y,z] on each edge, "
        "radius_mm) or chamfer (spec: edge_points_mm, distance_mm). Checks the spec and, for fillet and chamfer, that "
        "each point is on exactly one edge, before writing. After writing, the feature must regenerate, a fillet or "
        "chamfer must make a face per edge, and the feature list must be the old one plus this feature; otherwise the "
        "feature is deleted again. Returns the integrity result." + UNVERIFIED,
        {
            "document_id": ID,
            "workspace_id": ID,
            "element_id": ID,
            "expected_document_name": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": sorted(FEATURE_BUILDERS)},
            "spec": {"type": "object"},
        },
        ["document_id", "workspace_id", "element_id", "expected_document_name", "kind", "spec"],
        read_only=False,
    )
    def add_feature(args: Dict[str, Any]) -> Dict[str, Any]:
        did, wid, eid, kind = args["document_id"], args["workspace_id"], args["element_id"], args["kind"]
        protected.check_writable(did, wid)
        require_name(did, args["expected_document_name"])
        if kind not in FEATURE_BUILDERS or not isinstance(args["spec"], dict):
            raise ToolError("kind must be sketch, extrude, fillet or chamfer, with a spec object. Nothing was written.")
        feature = FEATURE_BUILDERS[kind](args["spec"])  # validates the whole spec before any call
        base = f"/partstudios/d/{did}/w/{wid}/e/{eid}"
        before, before_response = snapshot(did, "w", wid, eid)
        if before["features"] is None:
            raise ToolError("Onshape returned no feature list for this Part Studio, so the bridge cannot check the step. Nothing was written.")
        if kind == "extrude":
            sketch_id = args["spec"]["sketch_feature_id"]
            if not any(f["feature_id"] == sketch_id and f["type"] == "newSketch" for f in before["features"]):
                raise ToolError(f"No sketch with feature id {sketch_id} in this Part Studio. Nothing was written.")
        points: List[Tuple[float, float, float]] = []
        if kind in ("fillet", "chamfer"):
            points = edge_points(args["spec"])
            problems = []
            for point in points:
                answer = client().request("POST", base + "/featurescript", body={"script": count_script(edge_at(point)), "queries": []})
                count = fs_number(answer)
                if count != 1:
                    problems.append(f"{list(point)}: {'unknown' if count is None else int(count)} edges")
            if problems:
                raise ToolError(
                    "Each edge point must lie on exactly one solid edge, so the right edges take the "
                    f"{kind}: {'; '.join(problems)}. Nothing was written."
                )

        answer = client().request(
            "POST",
            base + "/features",
            body={
                "btType": "BTFeatureDefinitionCall-1406",
                "feature": feature,
                "serializationVersion": (before_response or {}).get("serializationVersion"),
                "sourceMicroversion": (before_response or {}).get("sourceMicroversion"),
            },
        ) or {}
        feature_id = (answer.get("feature") or {}).get("featureId") if isinstance(answer, dict) else None
        status = (answer.get("featureState") or {}).get("featureStatus") if isinstance(answer, dict) else None

        def roll_back(reason: str) -> None:
            undo_note = "The feature was deleted again, so the Part Studio is as it was before this call."
            try:
                if not feature_id:
                    raise ToolError("Onshape returned no feature id to delete")
                client().request("DELETE", f"{base}/features/featureid/{feature_id}")
                again, _ = snapshot(did, "w", wid, eid)
                if again["features"] != before["features"]:
                    raise ToolError("the feature list after deleting it differs from the list before")
            except ToolError as err:
                protected.retire_copy(did, f"a {kind} failed and could not be removed")
                raise ToolError(
                    f"{reason} Removing it did not succeed ({err}), so this document is in an unknown state and the bridge "
                    "will not write to it again. Make a new one."
                ) from err
            raise ToolError(f"{reason} {undo_note}")

        if status not in GOOD_STATUSES:
            roll_back(f"The {kind} did not regenerate (status {status!r}).")
        made_faces = None
        if points:
            made_faces = fs_number(
                client().request(
                    "POST",
                    base + "/featurescript",
                    body={"script": count_script(f'qCreatedBy(makeId("{feature_id}"), EntityType.FACE)'), "queries": []},
                )
            )
            if made_faces is None or made_faces < len(points):
                roll_back(
                    f"The {kind} made {'an unknown number of' if made_faces is None else int(made_faces)} face(s) for "
                    f"{len(points)} edge(s), so not every edge took it."
                )
        after, after_response = snapshot(did, "w", wid, eid)
        health = feature_health(after_response)
        if health["regenerated"] is not True:
            roll_back("After the change, the Part Studio did not regenerate cleanly, or its health is unknown.")
        integrity = integrity_compare(before, after, [feature_id])
        if integrity["result"] == "foreign":
            protected.retire_copy(did, "unexpected features appeared during a build step")
            raise ToolError(
                "The feature list after this step holds features this step did not make. The step fails; the bridge will "
                f"not write to this document again. {json.dumps({k: integrity[k] for k in ('unexpected_features', 'missing_features')})}"
            )
        return {
            "feature_id": feature_id,
            "name": feature["name"],
            "status": status,
            "faces_made": made_faces,
            "health": health,
            "document_integrity": integrity,
        }

    @server.tool(
        "onshape_import_step",
        "Import a customer's STEP file (rfq-to-quote path C1) into a NEW private document, as a Part Studio, and "
        "register the document as one the bridge may write. The file must be on the machine running the bridge. "
        "Polls the translation for up to about two minutes; if it is still running, call onshape_import_status. "
        "Nothing is built on the imported bodies: read them with onshape_read_faces." + UNVERIFIED,
        {"file_path": {"type": "string", "minLength": 1}, "name": {"type": "string", "minLength": 1}},
        ["file_path", "name"],
        read_only=False,
    )
    def import_step(args: Dict[str, Any]) -> Dict[str, Any]:
        source = Path(args["file_path"])
        if source.suffix.lower() not in (".step", ".stp"):
            raise ToolError("file_path must be a .step or .stp file. Nothing was imported.")
        if not source.is_file():
            raise ToolError(f"No file at {source}. Nothing was imported.")
        size = source.stat().st_size
        if size == 0 or size > MAX_IMPORT_BYTES:
            raise ToolError(f"The file is {size} bytes; the bridge imports files from 1 byte to {MAX_IMPORT_BYTES} bytes. Nothing was imported.")
        content = source.read_bytes()
        did, wid = new_document(args["name"], "imported", {"source_file": str(source), "source_sha256": hashlib.sha256(content).hexdigest()})
        start_ids = {e.get("id") for e in part_studios(did, wid)}
        protected.copies[did]["preexisting_part_studios"] = sorted(i for i in start_ids if i)
        raw = multipart_body(
            {"translate": "true", "flattenAssemblies": "true", "storeInDocument": "true"},
            "file",
            source.name,
            content,
        )
        try:
            started = client().request("POST", f"/translations/d/{did}/w/{wid}", raw=raw) or {}
        except ToolError:
            protected.retire_copy(did, "the STEP upload failed")
            raise
        translation_id = started.get("id") if isinstance(started, dict) else None
        if not translation_id:
            protected.retire_copy(did, "the STEP import did not start")
            raise ToolError("Onshape did not start the translation (no translation id). The new document will not be used.")
        protected.copies[did]["translation_id"] = translation_id
        protected._save()
        return poll_import(did, polls=40)

    def poll_import(did: str, polls: int) -> Dict[str, Any]:
        record = protected.copies.get(did)
        if not record or record.get("kind") != "imported" or not record.get("translation_id"):
            raise ToolError(f"Document {did} is not a STEP import this bridge started.")
        if record.get("retired"):
            raise ToolError(f"The import into {did} was abandoned ({record['retired']}).")
        state_now: Dict[str, Any] = {}
        for attempt in range(polls):
            state_now = client().request("GET", f"/translations/{record['translation_id']}") or {}
            if state_now.get("requestState") in ("DONE", "FAILED"):
                break
            if attempt < polls - 1:
                client().sleep(3)
        request_state = state_now.get("requestState")
        if request_state == "FAILED":
            protected.retire_copy(did, "the STEP import failed")
            raise ToolError(f"Onshape could not import the STEP file: {state_now.get('failureReason')}. Fall back to reading the file as text, unverified.")
        if request_state != "DONE":
            return {"document_id": did, "state": request_state or "unknown", "next": "Still translating: call onshape_import_status."}
        wid = record["workspace_id"]
        before = set(record.get("preexisting_part_studios") or [])
        result_ids = set(state_now.get("resultElementIds") or [])
        studios = [e for e in part_studios(did, wid) if e.get("id") not in before or e.get("id") in result_ids]
        if len(studios) != 1:
            protected.retire_copy(did, f"the import made {len(studios)} Part Studios, not one")
            raise ToolError(
                f"The import finished but made {len(studios)} Part Studios, not one, so the bridge cannot tell which holds "
                "the customer's bodies. Fall back to reading the file as text, unverified."
            )
        eid = studios[0].get("id")
        record["part_studio_id"] = eid
        protected._save()
        details = client().request("GET", f"/partstudios/d/{did}/w/{wid}/e/{eid}/bodydetails")
        bodies = summarize_bodies(details)
        if not bodies:
            raise ToolError("The import finished but Onshape lists no bodies in the new Part Studio. Treat the import as failed.")
        return {"document_id": did, "workspace_id": wid, "element_id": eid, "state": "DONE", "bodies": bodies,
                "origin": "customer_step", "next": "Read faces with onshape_read_faces; build nothing on these bodies."}

    @server.tool(
        "onshape_import_status",
        "Check a STEP import that onshape_import_step left translating, and finish it when Onshape is done." + UNVERIFIED,
        {"document_id": ID},
        ["document_id"],
        read_only=False,
    )
    def import_status(args: Dict[str, Any]) -> Dict[str, Any]:
        return poll_import(args["document_id"], polls=1)

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
