"""A minimal MCP server over stdio, standard library only.

The CAD bridges in these plugins ship without third-party MCP packages so a
plant can run them with nothing but Python (and, for SOLIDWORKS, pywin32).
This module implements the small part of the Model Context Protocol they
need: initialize, ping, tools/list and tools/call, as newline-delimited
JSON-RPC 2.0 on stdin and stdout.

Logging goes to stderr only. Anything written to stdout that is not a
JSON-RPC frame corrupts the session.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TextIO

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """An error the tool reports to the model as a result with isError true.

    Use it for every expected failure: a precondition not met, a document
    that is not the one expected, a refused write. The model reads the
    message, so write it as an instruction a person can act on.
    """


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    read_only: bool = True

    def listing(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": False,
            },
        }


@dataclass
class Server:
    name: str
    version: str
    instructions: str = ""
    tools: Dict[str, Tool] = field(default_factory=dict)

    def tool(
        self,
        name: str,
        description: str,
        properties: Optional[Dict[str, Any]] = None,
        required: Optional[List[str]] = None,
        read_only: bool = True,
    ) -> Callable[[Callable[[Dict[str, Any]], Any]], Callable[[Dict[str, Any]], Any]]:
        schema: Dict[str, Any] = {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required

        def register(fn: Callable[[Dict[str, Any]], Any]) -> Callable[[Dict[str, Any]], Any]:
            self.tools[name] = Tool(name, description, schema, fn, read_only)
            return fn

        return register

    # -- JSON-RPC -----------------------------------------------------------

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the response for one message, or None for a notification."""
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message

        if method is None:
            # A response to something we never send. Ignore it.
            return None

        try:
            result = self._dispatch(method, message.get("params") or {})
        except _RpcError as err:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": err.code, "message": err.message}}

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            result: Dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return result
        if method == "ping":
            return {}
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return {"tools": [t.listing() for t in self.tools.values()]}
        if method == "tools/call":
            return self._call_tool(params)
        raise _RpcError(-32601, f"Method not found: {method}")

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name)
        if tool is None:
            raise _RpcError(-32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        missing = [k for k in tool.input_schema.get("required", []) if k not in arguments]
        if missing:
            return _text_result({"error": f"Missing required argument(s): {', '.join(missing)}"}, True)
        unknown = [k for k in arguments if k not in tool.input_schema["properties"]]
        if unknown:
            return _text_result({"error": f"Unknown argument(s): {', '.join(unknown)}"}, True)
        try:
            value = tool.handler(arguments)
        except ToolError as err:
            return _text_result({"error": str(err)}, True)
        except Exception as err:  # noqa: BLE001 - reported to the model, logged in full
            traceback.print_exc(file=sys.stderr)
            return _text_result({"error": f"{type(err).__name__}: {err}"}, True)
        return _text_result(value, False)

    # -- loop ---------------------------------------------------------------

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if isinstance(message, list):
                replies = [r for r in (self.handle(m) for m in message) if r is not None]
                if replies:
                    _write(stdout, replies)
                continue
            reply = self.handle(message)
            if reply is not None:
                _write(stdout, reply)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text_result(value: Any, is_error: bool) -> Dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _write(stdout: TextIO, payload: Any) -> None:
    stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stdout.flush()


def log(*parts: Any) -> None:
    print(*parts, file=sys.stderr, flush=True)
