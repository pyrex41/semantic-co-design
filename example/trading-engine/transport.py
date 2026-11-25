"""Generic TLS HTTP, WebSocket and scoped HMAC signing driver."""

import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.client import HTTPSConnection, HTTPException
import json
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlencode, urlsplit

from values import canonical


MAX_WIRE = 2 * 1024 * 1024
TIMEOUT = 5
UTC_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z")


def credential(path):
    if path is None:
        return None
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise PermissionError("signing file must be owner-readable only")
    info = json.loads(path.read_text())
    if not all(isinstance(info.get(k), str) and info[k] and "\n" not in info[k] and "\r" not in info[k]
               for k in ("key", "secret", "passphrase")):
        raise ValueError("signing handle has invalid fields")
    if len(base64.b64decode(info["secret"], validate=True)) != 64:
        raise ValueError("signing handle must wrap a 64-byte key")
    if type(info.get("epoch")) is not int or not isinstance(info.get("scope"), str):
        raise ValueError("signing handle requires scope and epoch")
    return info


def signature(request, credential_info, method, resource, data):
    if not request["private"]:
        return {}
    if (credential_info is None or credential_info["scope"] != request["scope"]
            or credential_info["epoch"] != request["epoch"]):
        raise PermissionError("signing handle does not match request scope and epoch")
    if request["auth"]["scheme"] != "hmac-sha256/base64":
        raise ValueError("unsupported signing algorithm")
    now = str(int(time.time()))
    preimage = f"{now}{method}{resource}".encode() + data
    digest = hmac.new(base64.b64decode(credential_info["secret"], validate=True), preimage, hashlib.sha256).digest()
    values = {"timestamp": now, "signature": base64.b64encode(digest).decode(),
              "key": credential_info["key"], "passphrase": credential_info["passphrase"]}
    return {key: values[value] for key, value in request["auth"]["fields"].items()}


def read_response(raw):
    if len(raw) > MAX_WIRE:
        raise ValueError("wire response exceeds size bound")
    return json.loads(raw)


def epoch_millis(text):
    match = UTC_TIMESTAMP.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    try:
        seconds = datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    return int(seconds) * 1000 + int((match[2] or "")[:3].ljust(3, "0"))


def http(request, credential_info=None):
    endpoint = urlsplit(request["endpoint"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.path not in ("", "/"):
        raise ValueError("invalid TLS endpoint")
    path = request["path"]
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("invalid HTTP resource")
    qs = urlencode({key: value for key, value in request["query"].items() if value is not None})
    resource = f"{path}?{qs}" if qs else path
    data = b"" if request["body"] is None else canonical(request["body"]).encode()
    method = request["method"]
    try:
        auth = signature(request, credential_info, method, resource, data)
    except PermissionError:
        return {"network": "not-sent", "status": 0, "body": None, "headers": {}}
    headers = {"User-Agent": "generated-application/1", "Accept": "application/json", **auth}
    if data:
        headers["Content-Type"] = "application/json"
    connection = HTTPSConnection(endpoint.hostname, endpoint.port or 443, timeout=TIMEOUT,
                                 context=ssl.create_default_context())
    try:
        try:
            connection.connect()
        except (OSError, HTTPException):
            return {"network": "not-sent", "status": 0, "body": None, "headers": {}}
        try:
            connection.request(method, resource, body=data or None, headers=headers)
            reply = connection.getresponse()
            raw = reply.read(MAX_WIRE + 1)
            returned = {field: reply.getheader(field) for field in request.get("response-headers", [])}
            try:
                body = read_response(raw)
            except (ValueError, UnicodeError):
                body = None
            return {"network": "response", "status": reply.status, "body": body, "headers": returned}
        except (OSError, HTTPException):
            return {"network": "unknown", "status": 0, "body": None, "headers": {}}
    finally:
        connection.close()


def frames(request, credential_info, limit):
    """Yield decoded frames from a bounded session using the runtime WebSocket library."""
    import websocket

    endpoint = urlsplit(request["endpoint"])
    if endpoint.scheme != "wss" or not endpoint.hostname:
        raise ValueError("invalid WebSocket endpoint")
    subscription = {**request["body"], **signature(request, credential_info, request["sign-method"], request["path"], b"")}
    connection = websocket.create_connection(request["endpoint"], timeout=TIMEOUT, enable_multithread=False)
    try:
        connection.send(canonical(subscription))
        for _ in range(limit):
            yield read_response(connection.recv())
    finally:
        connection.close()
