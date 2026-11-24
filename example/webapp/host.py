"""Generic HTTP, form, schema and process host for a generated application release."""
import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
from collections import Counter
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import psycopg

from drivers import Context
from values import Problem, element, json_response, parse_int, response


LOG = logging.getLogger("application")
FIELD = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TYPES = {"text": "string", "integer": "integer", "email": "string", "file": "string",
         "choice": "string", "textarea": "string", "json": "object", "hidden": "string"}
MAX_BODY = 8 * 1024 * 1024


def problem(status, code, detail):
    return Problem(status, code, detail)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise problem(400, "duplicate-field", "A request field was supplied more than once.")
        result[key] = value
    return result


def no_constant(_):
    raise ValueError("non-finite number")


def read_json(raw):
    return json.loads(raw, object_pairs_hook=unique, parse_constant=no_constant)


def decode_body(content_type, raw):
    media = content_type.split(";", 1)[0].lower()
    if not raw:
        return {}
    try:
        if media == "application/json":
            body = read_json(raw)
            if not isinstance(body, dict):
                raise problem(400, "expected-object", "Expected a JSON object.")
            return body
        if media == "application/x-www-form-urlencoded":
            return unique(parse_qsl(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=64))
        if media == "multipart/form-data":
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw
            )
            if not message.is_multipart() or message.defects:
                raise ValueError("bad multipart")
            parts = []
            for item in message.iter_parts():
                name = item.get_param("name", header="Content-Disposition")
                data = item.get_payload(decode=True)
                if not name or item.is_multipart() or not isinstance(data, bytes) or len(parts) >= 64:
                    raise ValueError("bad multipart")
                parts.append((name, data if item.get_filename() is not None else data.decode()))
            return unique(parts)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise problem(400, "malformed-input", "The request could not be decoded.") from exc
    raise problem(415, "content-type", "Use JSON or a supported browser form encoding.")


def decode_fields(body, fields, browser):
    unknown = set(body) - fields.keys() - ({"csrf", "command_key"} if browser else set())
    if unknown:
        raise problem(422, "unknown-field", "Unexpected field " + sorted(unknown)[0] + ".")
    values = {}
    for name, field in fields.items():
        value = body.get(name, field.get("default"))
        if value is None or (browser and value == "" and not field.get("required", True)):
            if field.get("required", True):
                raise problem(422, "missing-field", "Fill in " + field.get("label", name) + ".")
            values[name] = field.get("default")
            continue
        typ = field["type"]
        if typ == "integer":
            if not browser and type(value) is not int:
                raise problem(422, "invalid-integer", field.get("label", name) + " must be a whole number.")
            value = parse_int(value)
        elif typ == "json":
            if browser and isinstance(value, str):
                try:
                    value = read_json(value)
                except ValueError as exc:
                    raise problem(422, "invalid-json", field.get("label", name) + " must contain valid JSON.") from exc
            if not isinstance(value, dict):
                raise problem(422, "expected-object", field.get("label", name) + " must be an object.")
        elif typ == "file":
            if not isinstance(value, (bytes, str)):
                raise problem(422, "invalid-bytes", field.get("label", name) + " must contain a file.")
        elif not isinstance(value, str) or "\0" in value:
            raise problem(422, "expected-text", field.get("label", name) + " must be text.")
        if isinstance(value, str) and len(value) > field.get("max", MAX_BODY):
            raise problem(422, "input-length", field.get("label", name) + " is too long.")
        if typ == "choice" and value not in field["values"]:
            raise problem(422, "invalid-choice", "Select a value for " + field.get("label", name) + ".")
        values[name] = value
    return values


def html_form(route, params, hidden, values):
    action = FIELD.sub(lambda match: str(params[match[1]]), route["path"])
    children = [element("input", {"type": "hidden", "name": key, "value": value}, [])
                for key, value in hidden.items()]
    for name, field in route.get("fields", {}).items():
        shape = field["type"]
        value = values.get(name, field.get("default"))
        control = {"name": name, "id": route["name"] + "-" + hashlib.sha256(action.encode()).hexdigest()[:8] + "-" + name,
                   "required": "required" if field.get("required", True) else None}
        if field.get("max") and shape != "integer":
            control["maxlength"] = field["max"]
        if shape == "hidden" or field.get("hidden"):
            children.append(element("input", {**control, "type": "hidden", "value": value}, []))
            continue
        label = element("label", {"for": control["id"]}, field.get("label", name))
        if shape in ("textarea", "json"):
            formatted = json.dumps(value, sort_keys=True) if shape == "json" and value is not None else value
            inp = element("textarea", {**control, "rows": field.get("rows", 4)}, formatted)
        elif shape == "choice":
            inp = element("select", control, [
                element("option", {"value": choice, "selected": "selected" if choice == value else None},
                        choice.capitalize())
                for choice in field["values"]
            ])
        else:
            inp = element("input", {**control, "type": {"integer": "number", "file": "file"}.get(shape, shape),
                                    "value": None if shape == "file" else value}, [])
        children.append(element("div", {"class": "field"}, [label, inp]))
    children.append(element("button", {"type": "submit"}, route["button"]))
    return element("form", {
        "method": "post", "action": action, "class": "action-form",
        "enctype": "multipart/form-data" if any(f["type"] == "file" for f in route["fields"].values()) else None
    }, children)


class Surface:
    def __init__(self, manifest, entries, root, settings, policy_command):
        self.manifest, self.entries, self.root, self.settings = manifest, entries, root, settings
        self.policy_command = policy_command
        self.routes = [(self.pattern(route["path"]), route) for route in manifest["routes"]]
        self.named = {route["name"]: route for _, route in self.routes}
        self.counts = Counter()
        self.duration = Counter()
        self.lock = threading.Lock()

    @staticmethod
    def pattern(path):
        parts = []
        offset = 0
        for match in FIELD.finditer(path):
            parts.extend((re.escape(path[offset:match.start()]), f"(?P<{match[1]}>[A-Za-z0-9_-]{{1,128}})"))
            offset = match.end()
        parts.append(re.escape(path[offset:]))
        return re.compile("".join(parts))

    def route(self, method, path):
        allowed = set()
        for pattern, route in self.routes:
            matched = pattern.fullmatch(path)
            if matched:
                if route["method"] == method:
                    return route, matched.groupdict()
                allowed.add(route["method"])
        if allowed:
            raise problem(405, "method-not-allowed", "This method is unavailable for the path.")
        raise problem(404, "not-found", "The requested page does not exist.")

    def form(self, route, params, hidden, values):
        return html_form(self.named[route], params, hidden, values)

    def schema(self):
        paths = {}
        for _, route in self.routes:
            if route["mode"] != "api":
                continue
            operation = {
                "operationId": route["name"], "summary": route["summary"],
                "parameters": [{"name": name, "in": "path", "required": True,
                                "schema": {"type": "string"}} for name in FIELD.findall(route["path"])],
                "responses": {str(status): {"description": HTTPStatus(status).phrase}
                              for status in route.get("statuses", [200, 400, 401, 403, 404, 409, 422])},
            }
            fields = route.get("fields")
            if route.get("idempotent"):
                operation["parameters"].extend([
                    {"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "X-CSRF-Token", "in": "header", "required": False, "schema": {"type": "string"}},
                ])
            if fields:
                schema = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
                for name, field in fields.items():
                    field_schema = {"type": TYPES[field["type"]]}
                    if field["type"] == "file":
                        field_schema["contentEncoding"] = "base64"
                    if field["type"] == "choice":
                        field_schema["enum"] = field["values"]
                    if "max" in field:
                        field_schema["maxLength"] = field["max"]
                    schema["properties"][name] = field_schema
                    if field.get("required", True):
                        schema["required"].append(name)
                operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
            paths.setdefault(route["path"], {})[route["method"].lower()] = operation
        return {"openapi": "3.1.0", "info": {"title": self.manifest["name"], "version": self.manifest["version"]},
                "paths": paths}

    def metrics(self):
        rows = ['# TYPE http_requests_total counter', '# TYPE http_request_seconds_sum counter']
        with self.lock:
            for key, count in sorted(self.counts.items()):
                route, status = key
                rows.append(f'http_requests_total{{route="{route}",status="{status}"}} {count}')
                rows.append(f'http_request_seconds_sum{{route="{route}",status="{status}"}} {self.duration[key]:.6f}')
        return "\n".join(rows) + "\n"

    def invoke(self, entry, arg, transactional=False):
        with Context(self.settings, self.root, self.policy_command) as ctx:
            ctx.operations.update({"form": self.form, "api-schema": self.schema, "metrics": self.metrics})
            if transactional:
                ctx.begin()
            return self.entries[entry](ctx, arg)

    def asset(self, path):
        if path.startswith("/assets/") and path[8:] in self.manifest["assets"]:
            target = self.root / path[8:]
            return response(200, "text/css; charset=utf-8", target.read_bytes(), {})
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ApplicationHost"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass

    def request_body(self, route):
        if self.headers.get("Transfer-Encoding"):
            raise problem(400, "transfer-encoding", "Send a Content-Length instead of transfer encoding.")
        content_length = self.headers.get_all("Content-Length", [])
        if len(content_length) > 1 or (content_length and not re.fullmatch(r"\d{1,10}", content_length[0])):
            raise problem(400, "content-length", "Send exactly one valid Content-Length.")
        length = int(content_length[0]) if content_length else 0
        if length > route.get("max_body", MAX_BODY):
            raise problem(413, "too-large", "The request exceeds the size limit.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise problem(400, "incomplete", "The request body ended early.")
        return decode_body(self.headers.get("Content-Type", ""), raw)

    def dispatch(self):
        surface = self.server.surface
        began = time.monotonic()
        route, params = {}, {}
        try:
            try:
                path = urlsplit(self.path)
            except ValueError as exc:
                raise problem(400, "invalid-url", "The request URL is invalid.") from exc
            if path.scheme or path.netloc:
                raise problem(400, "invalid-url", "Send a path relative to the service.")
            asset = surface.asset(path.path) if self.command == "GET" else None
            if asset:
                self.write_result(asset)
                return
            route, params = surface.route(self.command, path.path)
            try:
                query = unique(parse_qsl(path.query, keep_blank_values=True, max_num_fields=32))
            except ValueError as exc:
                raise problem(400, "query", "The query could not be decoded.") from exc
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            browser = route["mode"] == "page"
            body = self.request_body(route)
            args = {
                "params": params, "query": query, "browser": browser,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "cookies": {name: item.value for name, item in cookie.items()},
                "body": decode_fields(body, route["fields"], browser) if "fields" in route else body,
                "csrf": body.get("csrf") if browser else self.headers.get("X-CSRF-Token"),
                "key": body.get("command_key") if browser else self.headers.get("Idempotency-Key"),
            }
            result = surface.invoke(route["entry"], args, route.get("transaction", True))
        except Problem as err:
            result = self.error_result(surface, route, params, err)
        except (KeyError, OSError, RuntimeError, ValueError, psycopg.Error) as err:
            LOG.error("request_failed route=%s kind=%s", route.get("name", "unmatched"), type(err).__name__)
            result = self.error_result(surface, route, params,
                                       problem(503, "unavailable", "The service could not complete the request."))
        key = (route.get("name", "unmatched"), result["status"])
        with surface.lock:
            surface.counts[key] += 1
            surface.duration[key] += time.monotonic() - began
        LOG.info("request route=%s status=%d elapsed_ms=%d", key[0], key[1], (time.monotonic() - began) * 1000)
        try:
            self.write_result(result)
        except OSError:
            pass

    @staticmethod
    def error_result(surface, route, params, err):
        entry = surface.manifest["processes"].get("page_error")
        if entry and route.get("mode") == "page":
            return surface.invoke(entry, {"status": err.status, "title": HTTPStatus(err.status).phrase,
                                         "detail": err.detail, "params": params})
        return json_response(err.status, {"error": {"code": err.code, "detail": err.detail}})

    def write_result(self, result):
        data = result["body"].encode() if isinstance(result["body"], str) else result["body"]
        self.send_response(result["status"])
        headers = {
            "Content-Type": result["type"], "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'",
            "Connection": "close", **result["headers"]
        }
        for name, value in headers.items():
            for item in value if isinstance(value, list) else [value]:
                if "\r" in item or "\n" in item:
                    raise ValueError("invalid response header")
                self.send_header(name, item)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = dispatch


class Server(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(self, address, surface):
        self.surface = surface
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main(command, manifest, entries, argv, root):
    logging.basicConfig(level=os.environ.get("SC_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    settings = dict(os.environ)
    policy_command = shlex.split(settings["SC_POLICY"]) if settings.get("SC_POLICY") else None
    surface = Surface(manifest, entries, root, settings, policy_command)
    parser = argparse.ArgumentParser(prog=command)
    if command == "serve":
        parser.add_argument("--listen", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8080)
        args = parser.parse_args(argv)
        origin = urlsplit(settings.get("SC_ORIGIN", ""))
        local = settings.get("SC_INSECURE_LOCAL") == "1"
        loopback = {"127.0.0.1", "localhost", "::1"}
        if (
            not origin.hostname or origin.path or origin.query or origin.fragment
            or origin.username is not None
            or (local and (origin.scheme != "http" or origin.hostname not in loopback or args.listen not in loopback))
            or (not local and origin.scheme != "https")
        ):
            parser.error("SC_ORIGIN must be an HTTPS origin or a test loopback origin")
        with Server((args.listen, args.port), surface) as server:
            print(f"Listening on http://{args.listen}:{server.server_port}", flush=True)
            server.serve_forever(poll_interval=0.2)
    elif command == "worker":
        parser.add_argument("--once", action="store_true")
        args = parser.parse_args(argv)
        while True:
            try:
                result = surface.invoke(manifest["processes"]["worker"], {})
                if args.once:
                    print(json.dumps(result))
                    return
                if not result.get("worked"):
                    time.sleep(1)
            except (OSError, psycopg.Error) as err:
                LOG.error("worker_failed kind=%s", type(err).__name__)
                if args.once:
                    raise SystemExit(1)
                time.sleep(2)
    else:
        commands = manifest["processes"]["admin"]
        parser.add_argument("operation", choices=commands)
        parser.add_argument("parameters", nargs="?", default="{}")
        args = parser.parse_args(argv)
        try:
            print(json.dumps(surface.invoke(commands[args.operation], read_json(args.parameters))))
        except Problem as err:
            print(f"{err.code}: {err.detail}", file=sys.stderr)
            raise SystemExit(2)


def native_main(command):
    from _application import entries
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text())
    main(command, manifest, entries, sys.argv[1:], root)
