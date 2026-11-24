"""Append-only files of checksummed records: the event log and the durable file.

A record is one line: eight hex digits (CRC-32 of the body), a space, the body
(compact JSON, which never contains a newline) and "\\n". A record is appended
with one write, so what a kill or a power loss can leave behind is a proper
prefix of the last record: bytes after the final newline. That is a *torn
tail*, and it is the only damage read() accepts. A complete line that fails
its checksum, anywhere in the file, is corruption.
"""
import fcntl
import json
import os
import zlib


class Corrupt(ValueError):
    pass


def line(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def frame(record):
    body = line(record)
    return b"%08x %s" % (zlib.crc32(body[:-1]), body)


def read(path):
    """(records, torn): every complete record, and the number of bytes of a torn tail."""
    data = path.read_bytes() if path.exists() else b""
    *lines, tail = data.split(b"\n")
    records = []
    for n, line in enumerate(lines):
        try:
            if len(line) < 9 or line[8:9] != b" " or int(line[:8], 16) != zlib.crc32(line[9:]):
                raise ValueError("checksum")
            records.append(json.loads(line[9:]))
        except ValueError:
            raise Corrupt(f"{path.name}: record {n} fails its checksum") from None
    return records, len(tail)


def repair(path):
    """Cut a torn tail off, durably. Returns the records that remain."""
    records, torn = read(path)
    if torn:
        with open(path, "r+b") as file:
            file.truncate(path.stat().st_size - torn)
            sync(file.fileno())
    return records


def sync(fd):
    """fsync; with HOST_FULLFSYNC set on Darwin also flush the drive's own cache."""
    if os.environ.get("HOST_FULLFSYNC") and hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)
