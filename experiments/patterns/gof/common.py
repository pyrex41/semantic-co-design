"""Validate the bounded values interpolated into generated C."""

import re


def fields(config, *names):
    if not isinstance(config, dict) or set(config) != set(names):
        raise ValueError(f"expected exactly these fields: {', '.join(names)}")


def word(value):
    if type(value) is not int or not 0 <= value < 2**32:
        raise ValueError(f"expected an unsigned 32-bit integer, got {value!r}")
    return value


def items(value, maximum=32):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"expected a list of at most {maximum} elements")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", value):
        raise ValueError(f"expected a lowercase C identifier suffix, got {value!r}")
    return value


def choice(value, *choices):
    if value not in choices:
        raise ValueError(f"expected one of {choices!r}, got {value!r}")
    return value
