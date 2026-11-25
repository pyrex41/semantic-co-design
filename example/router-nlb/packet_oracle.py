"""Independent wire fixtures for the router release checks."""

from ipaddress import ip_address
import struct


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    value = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while value >> 16:
        value = (value & 0xFFFF) + (value >> 16)
    return value ^ 0xFFFF


def mac(address):
    return bytes.fromhex(address.replace(":", ""))


def packed(address):
    return ip_address(address).packed


def ethernet(source, destination, kind, payload, vlan=None):
    header = mac(destination) + mac(source)
    if vlan is not None:
        header += struct.pack("!HH", 0x8100, vlan)
    return header + struct.pack("!H", kind) + payload


def ipv4(source, destination, protocol, payload, ttl=64, flags=0x4000):
    header = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, len(payload) + 20, 0, flags, ttl, protocol,
        0, packed(source), packed(destination),
    )
    return header[:10] + struct.pack("!H", checksum(header)) + header[12:] + payload


def ipv6(source, destination, protocol, payload, ttl=64):
    return struct.pack(
        "!IHBB16s16s", 0x60000000, len(payload), protocol, ttl,
        packed(source), packed(destination),
    ) + payload


def pseudo_header(source, destination, protocol, length):
    addresses = packed(source) + packed(destination)
    if len(addresses) == 8:
        return addresses + struct.pack("!BBH", 0, protocol, length)
    return addresses + struct.pack("!I3xB", length, protocol)


def udp(source, destination, sport, dport, payload=b"wire oracle", *, no_checksum=False):
    length = len(payload) + 8
    header = struct.pack("!4H", sport, dport, length, 0)
    check = 0 if no_checksum else checksum(pseudo_header(source, destination, 17, length) + header + payload) or 0xFFFF
    return header[:6] + struct.pack("!H", check) + payload


def tcp(source, destination, sport, dport, flags, sequence=7, acknowledgment=0, payload=b""):
    header = struct.pack("!HHIIBBHHH", sport, dport, sequence, acknowledgment, 0x50, flags, 65_535, 0, 0)
    check = checksum(pseudo_header(source, destination, 6, len(header) + len(payload)) + header + payload)
    return header[:16] + struct.pack("!H", check) + header[18:] + payload


def arp(operation, sender_mac, sender_ip, target_mac, target_ip):
    return struct.pack(
        "!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, operation,
        mac(sender_mac), packed(sender_ip), mac(target_mac), packed(target_ip),
    )


def ndp(kind, source, destination, target, link_mac, flags=0):
    option_kind = 1 if kind == 135 else 2
    body = struct.pack("!BBHI16sBB6s", kind, 0, 0, flags, packed(target), option_kind, 1, mac(link_mac))
    check = checksum(pseudo_header(source, destination, 58, len(body)) + body)
    return body[:2] + struct.pack("!H", check) + body[4:]


def icmp_error(kind, code, original, *, parameters=0, source=None, destination=None):
    body = struct.pack("!BBHI", kind, code, 0, parameters) + original
    prefix = pseudo_header(source, destination, 58, len(body)) if source else b""
    return body[:2] + struct.pack("!H", checksum(prefix + body)) + body[4:]
