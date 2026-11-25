"""Value and response ABI shared by the reference and native application."""
import base64
import binascii
import hashlib
import hmac
import html
import json
import operator
import re
from urllib.parse import quote


class Problem(Exception):
    def __init__(self, status, code, detail):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


class Markup(str):
    pass


def failure(status, code, detail):
    raise Problem(status, code, detail)


def same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    return left == right


def get(value, key):
    if isinstance(value, dict):
        return value.get(key)
    if type(key) is int and isinstance(value, (list, str)) and 0 <= key < len(value):
        return value[key]
    return None


def signed(value):
    if type(value) is not int or not -(2**63) <= value < 2**63:
        failure(422, "integer-range", "Expected a signed 64-bit integer.")
    return value


def integer_operation(operation):
    def checked(left, right):
        a, b = signed(left), signed(right)
        if b == 0 and operation in (operator.floordiv, operator.mod):
            failure(422, "zero-divisor", "The divisor must be nonzero.")
        return signed(operation(a, b))
    return checked


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    data = value if isinstance(value, bytes) else canonical(value).encode()
    return hashlib.sha256(data).hexdigest()


def binary(value):
    if isinstance(value, bytes):
        return value
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError, binascii.Error):
        failure(422, "invalid-bytes", "The file is not valid base64.")


def secure_equal(left, right):
    return isinstance(left, str) and isinstance(right, str) and hmac.compare_digest(left.encode(), right.encode())


def stringify(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return canonical(value)


def is_text(value, minimum, maximum):
    return (
        isinstance(value, str)
        and minimum <= len(value.strip()) <= maximum
        and len(value) <= maximum
        and "\0" not in value
    )


TAG = re.compile(r"[a-z][a-z0-9-]*\Z")
VOID = {"input", "link", "meta", "br", "hr"}


def render_content(value):
    if isinstance(value, Markup):
        return str(value)
    if isinstance(value, list):
        return "".join(render_content(item) for item in value)
    return html.escape(stringify(value))


def element(tag, attrs, children):
    if not TAG.fullmatch(tag) or any(not TAG.fullmatch(key) for key in attrs):
        raise ValueError("invalid HTML name")
    attributes = "".join(
        f' {key}="{html.escape(stringify(value), quote=True)}"'
        for key, value in attrs.items() if value is not None
    )
    opening = f"<{tag}{attributes}>"
    return Markup(opening if tag in VOID else f"{opening}{render_content(children)}</{tag}>")


def response(status, content_type, body, headers):
    return {"status": status, "type": content_type, "body": body, "headers": headers}


def html_response(status, value):
    return response(status, "text/html; charset=utf-8", "<!doctype html>\n" + render_content(value), {})


def json_response(status, value):
    return response(status, "application/json; charset=utf-8", canonical(value), {})


def redirect(location, cookies):
    return response(303, "text/plain; charset=utf-8", "", {"Location": location, "Set-Cookie": cookies})


def cookie(name, value, age, secure):
    if not re.fullmatch(r"[a-z0-9_-]+", name) or not re.fullmatch(r"[a-zA-Z0-9_-]*", value):
        raise ValueError("invalid cookie")
    return f"{name}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def email(value):
    return (
        isinstance(value, str) and len(value) <= 254
        and bool(re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value))
    )


def csv_cell(value):
    text = stringify(value)
    if text.startswith(("=", "-", "+", "@", "\t", "\r")):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def csv_table(columns, rows):
    return "\r\n".join([",".join(map(csv_cell, columns)),
        *(",".join(csv_cell(row[key]) for key in columns) for row in rows)]) + "\r\n"


def slug(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]|[a-z0-9]", value))


def parse_int(value):
    if type(value) is int:
        return signed(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,19}", value):
        return signed(int(value))
    failure(422, "invalid-integer", "Expected a whole number.")


PURE = {
    "=": (2, same), "<": (2, operator.lt), ">": (2, operator.gt), ">=": (2, operator.ge),
    "<=": (2, operator.le), "+": (2, integer_operation(operator.add)), "-": (2, integer_operation(operator.sub)),
    "*": (2, integer_operation(operator.mul)), "floor-div": (2, integer_operation(operator.floordiv)),
    "floor-mod": (2, integer_operation(operator.mod)),
    "not": (1, operator.not_), "truth": (1, bool),
    "get": (2, get), "obj": (1, lambda pairs: dict(pairs)), "merge": (2, lambda a, b: {**a, **b}),
    "obj?": (1, lambda a: isinstance(a, dict)), "keys": (1, lambda a: sorted(a)),
    "list?": (1, lambda a: isinstance(a, list)), "bool?": (1, lambda a: type(a) is bool),
    "str?": (1, lambda a: isinstance(a, str)),
    "put": (3, lambda a, k, v: {**a, k: v}),
    "remove": (2, lambda a, k: {key: val for key, val in a.items() if key != k}),
    "at": (2, get), "len": (1, len), "drop": (2, lambda a, n: a[n:]),
    "take": (2, lambda a, n: a[:n]), "prepend": (2, lambda a, b: [a, *b]),
    "contains": (2, lambda a, b: b in a), "append": (2, operator.add),
    "concat": (1, lambda a: "".join(map(stringify, a))), "str": (1, stringify),
    "int": (1, parse_int), "int?": (1, lambda a: type(a) is int),
    "text?": (3, is_text), "slug?": (1, slug), "email?": (1, email),
    "sha256?": (1, lambda a: isinstance(a, str) and bool(re.fullmatch(r"[0-9a-f]{64}", a))),
    "lower": (1, str.lower), "trim": (1, str.strip),
    "quote": (1, lambda text: quote(stringify(text), safe="")),
    "json": (1, canonical), "digest": (1, digest), "bytes": (1, binary),
    "secure=": (2, secure_equal), "failure": (3, failure),
    "element": (3, element), "html-response": (2, html_response), "json-response": (2, json_response),
    "response": (4, response), "redirect": (2, redirect), "cookie": (4, cookie), "csv-cell": (1, csv_cell), "csv-table": (2, csv_table),
}
functions = {name: function for name, (_, function) in PURE.items()}
