#!/usr/bin/env python3
"""Qualify reference and generated router releases over independent Ethernet fixtures."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
import json
import os
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from packet_oracle import arp, ethernet, icmp_error, ipv4, ipv6, ndp, packed, tcp, udp


HERE = Path(__file__).resolve().parent
ROUTER_FRONT = "02:00:00:00:00:01"
ROUTER_BACK = "02:00:00:00:00:02"
CLIENT = "02:00:00:00:00:0a"
BLUE = "02:00:00:00:00:14"
GREEN = "02:00:00:00:00:15"
PC4 = "192.0.2.10"
PC6 = "2001:db8:1::10"
BLUE4 = "198.51.100.20"
BLUE6 = "2001:db8:2::20"
GREEN4 = "198.51.100.21"
GREEN6 = "2001:db8:2::21"
VIP4 = "203.0.113.80"
VIP6 = "2001:db8:3::80"
SNAT4 = "198.51.100.100"
SNAT6 = "2001:db8:2::100"
NET4 = "203.0.113.89"
NET6 = "2001:db8:3::89"
TOPOLOGY = json.loads((HERE / "topology.json").read_text())


def rows(value):
    return {"port": value[0], "frame": list(value[1])}


class Mode:
    def __init__(self, command, db):
        self.command = [*command, "--db", str(db)]
        self.process = subprocess.Popen(
            [*self.command, "stream"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )

    def call(self, entry, arg=None, *, now=100):
        line = json.dumps({"entry": entry, "arg": arg or {}, "now": now})
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            raise AssertionError(f"no response to {entry}: {self.process.poll()}")
        return json.loads(response)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()


class Pair:
    def __init__(self, first, second, directory):
        self.modes = (
            Mode(first, directory / "reference.sqlite"),
            Mode(second, directory / "native.sqlite"),
        )
        self.calls = 0

    def call(self, entry, arg=None, *, now=100):
        reference, native = [mode.call(entry, arg, now=now) for mode in self.modes]
        assert reference == native, (entry, reference, native)
        self.calls += 1
        return native

    def frame(self, frame, reason, *tx, port="front", now=100):
        response = self.call("frame", {"port": port, "frame": list(frame)}, now=now)
        assert response["reason"] == reason, (response, reason)
        assert response["tx"] == list(map(rows, tx)), (response, tx)
        return response

    def close(self):
        for mode in self.modes:
            mode.close()


def frame4(source_mac, target_mac, source, target, protocol, payload, *, back=False, ttl=64):
    return ethernet(source_mac, target_mac, 0x800, ipv4(source, target, protocol, payload, ttl), 42 if back else None)


def frame6(source_mac, target_mac, source, target, protocol, payload, *, back=False, ttl=64):
    return ethernet(source_mac, target_mac, 0x86DD, ipv6(source, target, protocol, payload, ttl), 42 if back else None)


def tcp_syn(port, target=VIP4, ttl=64):
    return frame4(CLIENT, ROUTER_FRONT, PC4, target, 6, tcp(PC4, target, port, 443, 2), ttl=ttl)


def tcp_backend(port, backend_ip, backend_mac, *, ttl=63):
    return frame4(ROUTER_BACK, backend_mac, SNAT4, backend_ip, 6, tcp(SNAT4, backend_ip, port, 8443, 2), back=True, ttl=ttl)


def bgp_message(kind, payload=b""):
    return b"\xff" * 16 + struct.pack("!HB", len(payload) + 19, kind) + payload


def wire_peer(pair, payload=None, event="record", now=110):
    return pair.call("peer", {"peer": "peer-a", "event": event, "bytes": list(payload or b"")}, now=now)


def bgp_replay(pair):
    start = wire_peer(pair, event="connected")
    local_open = struct.pack("!BHH4sB", 4, 64512, 30, packed("192.0.2.1"), 0)
    assert start["send"] == [list(bgp_message(1, local_open))]
    unsupported = struct.pack("!BHH4sBBB", 4, 64513, 30, packed(BLUE4), 2, 1, 0)
    denied = wire_peer(pair, bgp_message(1, unsupported))
    assert denied["close"] and denied["send"] == [list(bgp_message(3, b"\2\4"))]
    wire_peer(pair, event="connected")
    open_options = b"\2\6\1\4\0\1\0\1"
    peer_open = struct.pack("!BHH4sB", 4, 64513, 30, packed(BLUE4), len(open_options)) + open_options
    assert wire_peer(pair, bgp_message(1, peer_open))["send"] == [list(bgp_message(4))]
    established = wire_peer(pair, bgp_message(4))
    assert established["reason"] == "established"
    assert bytes(established["send"][0]).endswith(b"\30\xc0\0\x02")
    prefix = bytes([24, 203, 0, 114])
    attrs = b"\x40\x01\x01\x00\x40\x02\x04\x02\x01" + struct.pack("!H", 64513) + b"\x40\x03\x04" + packed(BLUE4)
    announced = wire_peer(pair, bgp_message(2, struct.pack("!HH", 0, len(attrs)) + attrs + prefix))
    target = "203.0.114.60"
    datagram = udp(PC4, target, 12345, 53)
    request = frame4(CLIENT, ROUTER_FRONT, PC4, target, 17, datagram)
    expected = frame4(ROUTER_BACK, BLUE, PC4, target, 17, datagram, back=True, ttl=63)
    assert pair.frame(request, "forwarded", ("back", expected), now=110)["generation"] == announced["generation"]
    withdrawn = wire_peer(pair, bgp_message(2, struct.pack("!H", len(prefix)) + prefix + b"\0\0"), now=111)
    assert withdrawn["generation"] == announced["generation"] + 1
    assert pair.call("status", now=111)["peer-routes"] == 0
    unreachable = ipv4("192.0.2.1", PC4, 1, icmp_error(3, 0, request[14:42]))
    pair.frame(request, "no-route", ("front", ethernet(ROUTER_FRONT, CLIENT, 0x800, unreachable)), now=111)
    timeout = wire_peer(pair, event="tick", now=141)
    assert timeout["reason"] == "hold-timeout" and timeout["send"] == [list(bgp_message(3, b"\4\0"))]


def routing_replay(pair):
    assert pair.call("boot", {"config": TOPOLOGY})["reason"] == "configured"
    payload4 = udp(PC4, NET4, 21000, 80)
    request4 = frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, payload4)
    expected4 = frame4(ROUTER_BACK, BLUE, PC4, NET4, 17, payload4, back=True, ttl=63)
    pair.frame(request4, "forwarded", ("back", expected4))
    payload6 = udp(PC6, NET6, 22000, 80)
    request6 = frame6(CLIENT, ROUTER_FRONT, PC6, NET6, 17, payload6)
    expected6 = frame6(ROUTER_BACK, BLUE, PC6, NET6, 17, payload6, back=True, ttl=63)
    pair.frame(request6, "forwarded", ("back", expected6))

    pair.frame(request4[:13], "truncated-ethernet")
    pair.frame(ethernet(CLIENT, ROUTER_FRONT, 0x806, bytes(27)), "truncated-arp")
    pair.frame(request4[:33], "truncated-ipv4")
    pair.frame(request4[:-1], "truncated-ipv4")
    pair.frame(request6[:53], "truncated-ipv6")
    pair.frame(request6[:-1], "truncated-ipv6")
    pair.frame(bytes(9219), "frame-oversized")
    pair.frame(ethernet(CLIENT, ROUTER_FRONT, 0x800, b"", 42)[:17], "truncated-vlan")
    pair.frame(ethernet(CLIENT, ROUTER_FRONT, 0x800, request4[14:], 42), "ingress-vlan")
    pair.frame(frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 6, b"\0" * 19), "truncated-tcp")
    pair.frame(frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, b"\0" * 7), "truncated-udp")
    pair.frame(frame6(CLIENT, ROUTER_FRONT, PC6, NET6, 17, udp(PC6, NET6, 1111, 2222, no_checksum=True)), "transport-checksum")
    pair.frame(ethernet(CLIENT, ROUTER_FRONT, 0x800, ipv4(PC4, NET4, 17, payload4, flags=0x2000)), "ipv4-fragment")
    pair.frame(frame6(CLIENT, ROUTER_FRONT, PC6, NET6, 44, bytes(8)), "ipv6-extension")

    expired4 = frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, payload4, ttl=1)
    expired6 = frame6(CLIENT, ROUTER_FRONT, PC6, NET6, 17, payload6, ttl=1)
    reply4 = ipv4("192.0.2.1", PC4, 1, icmp_error(11, 0, expired4[14:42]))
    reply6 = ipv6("2001:db8:1::1", PC6, 58, icmp_error(3, 0, expired6[14:], source="2001:db8:1::1", destination=PC6))
    pair.frame(expired4, "hop-limit", ("front", ethernet(ROUTER_FRONT, CLIENT, 0x800, reply4)))
    pair.frame(expired6, "hop-limit", ("front", ethernet(ROUTER_FRONT, CLIENT, 0x86DD, reply6)))
    shots = [{"port": "front", "now": 101, "frame": list(expired4)} for _ in range(32)]
    bounded = pair.call("batch", {"frames": shots}, now=101)
    assert bounded["reasons"] == ["hop-limit"] * 32
    assert len(bounded["tx"]) == 32
    assert pair.frame(expired4, "hop-limit", now=101)["control-suppressed"]
    assert pair.call("status", now=101)["counters"]["control-suppressed"] == 1

    long4 = frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, udp(PC4, NET4, 21000, 80, b"Q" * 1480))
    long6 = frame6(CLIENT, ROUTER_FRONT, PC6, NET6, 17, udp(PC6, NET6, 22000, 80, b"Q" * 1480))
    response4 = ipv4("192.0.2.1", PC4, 1, icmp_error(3, 4, long4[14:42], parameters=1500))
    response6 = ipv6("2001:db8:1::1", PC6, 58, icmp_error(2, 0, long6[14:1246], parameters=1500, source="2001:db8:1::1", destination=PC6))
    pair.frame(long4, "mtu", ("front", ethernet(ROUTER_FRONT, CLIENT, 0x800, response4)))
    pair.frame(long6, "mtu", ("front", ethernet(ROUTER_FRONT, CLIENT, 0x86DD, response6)))

    generation = pair.call("status")["generation"]
    broken = deepcopy(TOPOLOGY)
    broken["routes"][0]["out"] = "absent"
    assert pair.call("candidate", {"config": broken, "generation": generation})["reason"] == "invalid-configuration"
    pair.frame(request4, "forwarded", ("back", expected4))
    changed = deepcopy(TOPOLOGY)
    changed["routes"].append({"prefix": list(packed(NET4)), "bits": 32, "out": "back", "via": list(packed(GREEN4)), "metric": 600})
    assert pair.call("candidate", {"config": changed, "generation": generation})["reason"] == "candidate-validated"
    pair.frame(request4, "forwarded", ("back", expected4))
    committed = pair.call("commit")
    assert committed == {"reason": "committed", "generation": generation + 1}
    alternate = frame4(ROUTER_BACK, GREEN, PC4, NET4, 17, payload4, back=True, ttl=63)
    items = [{"port": "front", "now": 100, "frame": list(request4)}, {"port": "front", "now": 100, "frame": list(request6)}]
    batch = pair.call("batch", {"frames": items})
    assert batch == {"reasons": ["forwarded", "forwarded"], "tx": [rows(("back", alternate)), rows(("back", expected6))], "generation": generation + 1}
    assert pair.call("rollback", {"generation": generation + 1})["reason"] == "rolled-back"
    pair.frame(request4, "forwarded", ("back", expected4))
    bgp_replay(pair)


def neighbor_replay(pair):
    config = deepcopy(TOPOLOGY)
    config["neighbors"] = []
    assert pair.call("boot", {"config": config})["reason"] == "configured"
    payload4 = udp(PC4, NET4, 7000, 7001)
    request4 = frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, payload4)
    solicited4 = ethernet(ROUTER_BACK, "ff:ff:ff:ff:ff:ff", 0x806, arp(1, ROUTER_BACK, "198.51.100.1", "00:00:00:00:00:00", BLUE4), 42)
    pair.frame(request4, "neighbor-pending", ("back", solicited4))
    pair.frame(request4, "neighbor-pending")
    reply4 = ethernet(BLUE, ROUTER_BACK, 0x806, arp(2, BLUE, BLUE4, ROUTER_BACK, "198.51.100.1"), 42)
    delivered4 = frame4(ROUTER_BACK, BLUE, PC4, NET4, 17, payload4, back=True, ttl=63)
    pair.frame(reply4, "neighbor-learned", ("back", delivered4), ("back", delivered4), port="back", now=101)
    pair.frame(request4, "forwarded", ("back", delivered4), now=160)
    pair.frame(request4, "neighbor-pending", ("back", solicited4), now=161)
    # The SNAT address must be discoverable without preloading an independent backend's ARP table.
    vip_probe = ethernet(BLUE, "ff:ff:ff:ff:ff:ff", 0x806, arp(1, BLUE, BLUE4, "00:00:00:00:00:00", SNAT4), 42)
    vip_reply = ethernet(ROUTER_BACK, BLUE, 0x806, arp(2, ROUTER_BACK, SNAT4, BLUE, BLUE4), 42)
    pair.frame(vip_probe, "neighbor-learned", ("back", vip_reply), ("back", delivered4), port="back", now=162)

    payload6 = udp(PC6, NET6, 7002, 7003)
    request6 = frame6(CLIENT, ROUTER_FRONT, PC6, NET6, 17, payload6)
    discovery6 = ndp(135, "2001:db8:2::1", "ff02::1:ff00:20", BLUE6, ROUTER_BACK)
    solicited6 = frame6(ROUTER_BACK, "33:33:ff:00:00:20", "2001:db8:2::1", "ff02::1:ff00:20", 58, discovery6, back=True, ttl=255)
    pair.frame(request6, "neighbor-pending", ("back", solicited6), now=162)
    advertise = ndp(136, BLUE6, "2001:db8:2::1", BLUE6, BLUE, 0x60000000)
    reply6 = frame6(BLUE, ROUTER_BACK, BLUE6, "2001:db8:2::1", 58, advertise, back=True, ttl=255)
    delivered6 = frame6(ROUTER_BACK, BLUE, PC6, NET6, 17, payload6, back=True, ttl=63)
    pair.frame(reply6, "neighbor-learned", ("back", delivered6), port="back", now=163)


def flow_replay(pair):
    config = deepcopy(TOPOLOGY)
    config["port-high"] = 40001
    udp_v4, tcp_v6 = deepcopy(config["listeners"])
    udp_v4.update(protocol=17, port=53)
    tcp_v6.update(protocol=6, port=443)
    for backend in udp_v4["backends"]:
        backend["port"] = 5353
    for backend in tcp_v6["backends"]:
        backend["port"] = 8443
    config["listeners"].extend((udp_v4, tcp_v6))
    pair.call("boot", {"config": config})
    pair.frame(tcp_syn(30000), "forwarded", ("back", tcp_backend(40000, BLUE4, BLUE)))
    returning = tcp(BLUE4, SNAT4, 8443, 40000, 0x12, sequence=77, acknowledgment=8)
    reply = frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 6, returning, back=True)
    expected = tcp(VIP4, PC4, 443, 30000, 0x12, sequence=77, acknowledgment=8)
    pair.frame(reply, "forwarded", ("front", frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, expected, ttl=63)), port="back")
    selector = ["main", 6, list(packed(VIP4)), 443]
    assert pair.call("drain", {"listener": selector, "backend": "blue", "value": True})["reason"] == "backend-updated"
    pair.frame(tcp_syn(30000), "forwarded", ("back", tcp_backend(40000, BLUE4, BLUE)))
    pair.frame(tcp_syn(30001), "forwarded", ("back", tcp_backend(40001, GREEN4, GREEN)))
    pair.frame(tcp_syn(30002), "port-exhausted")
    assert pair.call("status")["flows"] == 2

    v6_selector = ["main", 17, list(packed(VIP6)), 53]
    query = frame6(CLIENT, ROUTER_FRONT, PC6, VIP6, 17, udp(PC6, VIP6, 31111, 53))
    blue = frame6(ROUTER_BACK, BLUE, SNAT6, BLUE6, 17, udp(SNAT6, BLUE6, 40000, 5353), back=True, ttl=63)
    green = frame6(ROUTER_BACK, GREEN, SNAT6, GREEN6, 17, udp(SNAT6, GREEN6, 40000, 5353), back=True, ttl=63)
    pair.frame(query, "forwarded", ("back", blue), now=101)
    assert pair.call("health", {"listener": v6_selector, "backend": "blue6", "value": False}, now=102)["reason"] == "backend-updated"
    v6_reply = frame6(BLUE, ROUTER_BACK, BLUE6, SNAT6, 17, udp(BLUE6, SNAT6, 5353, 40000), back=True)
    expected6 = frame6(ROUTER_FRONT, CLIENT, VIP6, PC6, 17, udp(VIP6, PC6, 53, 31111), ttl=63)
    pair.frame(v6_reply, "forwarded", ("front", expected6), port="back", now=102)
    pair.frame(query, "forwarded", ("back", blue), now=131)
    pair.frame(query, "forwarded", ("back", green), now=161)
    pair.frame(v6_reply, "unbound-translation", port="back", now=162)
    assert pair.call("tick", now=401)["reason"] == "ready"
    assert pair.call("status", now=401)["flows"] == 0

    query4 = frame4(CLIENT, ROUTER_FRONT, PC4, VIP4, 17, udp(PC4, VIP4, 31112, 53, no_checksum=True))
    outgoing4 = frame4(ROUTER_BACK, BLUE, SNAT4, BLUE4, 17, udp(SNAT4, BLUE4, 40000, 5353, no_checksum=True), back=True, ttl=63)
    pair.frame(query4, "forwarded", ("back", outgoing4), now=402)
    back4 = frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 17, udp(BLUE4, SNAT4, 5353, 40000, no_checksum=True), back=True)
    reply4 = frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 17, udp(VIP4, PC4, 53, 31112, no_checksum=True), ttl=63)
    pair.frame(back4, "forwarded", ("front", reply4), port="back", now=403)
    query6 = frame6(CLIENT, ROUTER_FRONT, PC6, VIP6, 6, tcp(PC6, VIP6, 31113, 443, 2))
    outgoing6 = frame6(ROUTER_BACK, GREEN, SNAT6, GREEN6, 6, tcp(SNAT6, GREEN6, 40000, 8443, 2), back=True, ttl=63)
    pair.frame(query6, "forwarded", ("back", outgoing6), now=404)
    back6 = frame6(GREEN, ROUTER_BACK, GREEN6, SNAT6, 6, tcp(GREEN6, SNAT6, 8443, 40000, 0x12), back=True)
    reply6 = frame6(ROUTER_FRONT, CLIENT, VIP6, PC6, 6, tcp(VIP6, PC6, 443, 31113, 0x12), ttl=63)
    pair.frame(back6, "forwarded", ("front", reply6), port="back", now=405)


def availability(reference, native, directory):
    def verify(active, standby):
        assert active.call("boot", {"config": TOPOLOGY, "mode": "standby"})["reason"] == "configured"
        assert standby.call("boot", {"config": TOPOLOGY, "mode": "standby"})["reason"] == "configured"
        assert active.call("lease", {"epoch": 1, "until": 101}, now=100)["reason"] == "lease-active"
        abandoned = active.call("frame", {"port": "front", "frame": list(tcp_syn(30000))}, now=100)
        assert abandoned["reason"] == "replica-required" and abandoned["tx"] == []
        assert active.call("status", now=100)["flows"] == 0
        assert active.call("replica-ack", {"ack": abandoned["replicate"]["id"]}, now=101)["reason"] == "fenced"
        assert active.call("status", now=101)["flows"] == 0
        assert active.call("lease", {"epoch": 2, "until": 115}, now=102)["reason"] == "lease-active"
        proposed = active.call("frame", {"port": "front", "frame": list(tcp_syn(30001))}, now=102)
        assert proposed["reason"] == "replica-required" and proposed["tx"] == []
        ack = standby.call("replica", {"offer": proposed["replicate"]}, now=102)
        assert ack["reason"] == "replica-committed"
        assert standby.call("replica", {"offer": proposed["replicate"]}, now=103) == ack
        assert standby.call("status", now=103)["flows"] == 1
        wrong = active.call("replica-ack", {"ack": [2, 9999]}, now=103)
        assert wrong["reason"] == "unknown-replica-ack" and wrong["tx"] == []
        sent = active.call("replica-ack", {"ack": ack["ack"]}, now=104)
        # Abandoning an offer advances the selection serial. The port remains available.
        assert sent["tx"] == [rows(("back", tcp_backend(40000, GREEN4, GREEN)))]
        assert active.call("status", now=105)["flows"] == 1
        assert standby.call("lease", {"epoch": 2, "until": 130}, now=115)["reason"] == "stale-lease"
        assert standby.call("lease", {"epoch": 3, "until": 130}, now=115)["reason"] == "lease-active"
        response = tcp(GREEN4, SNAT4, 8443, 40000, 0x12)
        frame = list(frame4(GREEN, ROUTER_BACK, GREEN4, SNAT4, 6, response, back=True))
        assert active.call("frame", {"port": "back", "frame": frame}, now=115)["reason"] == "fenced"
        reverse = standby.call("frame", {"port": "back", "frame": frame}, now=116)
        expected = tcp(VIP4, PC4, 443, 30001, 0x12)
        assert reverse["tx"] == [rows(("front", frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, expected, ttl=63)))]
        assert standby.call("recover", now=117)["reason"] == "recovered"
        assert standby.call("status", now=117)["ready"] is False

    active = Pair(reference, native, directory / "active")
    standby = Pair(reference, native, directory / "standby")
    try:
        verify(active, standby)
        return active.calls + standby.calls
    finally:
        active.close()
        standby.close()


def durable_reservations(command, path):
    living = path / "before-crash.sqlite"
    disk_at_crash = path / "after-crash.sqlite"
    original = Mode(command, living)
    reverse = tcp(BLUE4, SNAT4, 8443, 40000, 0x12)
    incoming = frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 6, reverse, back=True)
    request = {"port": "back", "frame": list(incoming)}
    translated = tcp(VIP4, PC4, 443, 52301, 0x12)
    expected = [rows(("front", frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, translated, ttl=63)))]
    try:
        assert original.call("boot", {"config": TOPOLOGY})["reason"] == "configured"
        creation = original.call("stage-frame", {"port": "front", "frame": list(tcp_syn(52301))}, now=100)
        assert creation["durable"] is True and creation["tx"] == []
        assert original.call("resume", creation["resume"], now=100)["tx"] == [rows(("back", tcp_backend(40000, BLUE4, BLUE)))]
        # Copy the durable cut preceding a refresh. Later steps represent packets
        # received while an actual server's coalesced checkpoint is stalled.
        with sqlite3.connect(living) as source, sqlite3.connect(disk_at_crash) as destination:
            source.backup(destination)
            flow = json.loads(destination.execute("SELECT payload FROM application_state").fetchone()[0])["flows"][0]
            assert (flow["until"], flow["reserved-until"]) == (400, 430)
        refreshed = original.call("stage-frame", request, now=125)
        assert "durable" not in refreshed and refreshed["tx"] == expected
        extension = original.call("stage-frame", request, now=131)
        assert extension["durable"] is True and extension["tx"] == []
        assert original.call("resume", extension["resume"], now=131)["tx"] == expected
        with sqlite3.connect(living) as source:
            flow = json.loads(source.execute("SELECT payload FROM application_state").fetchone()[0])["flows"][0]
            assert (flow["until"], flow["reserved-until"]) == (431, 461)
    finally:
        original.close()
    recovered = Mode(command, disk_at_crash)
    try:
        # t=412 is after the saved idle expiry and before the refresh's real
        # idle expiry t=425. The original tuple must still own its port.
        assert recovered.call("recover", now=412)["reason"] == "recovered"
        competing = recovered.call("stage-frame", {"port": "front", "frame": list(tcp_syn(52302))}, now=412)
        assert competing["durable"] is True and competing["tx"] == []
        assert recovered.call("resume", competing["resume"], now=412)["tx"] == [rows(("back", tcp_backend(40001, GREEN4, GREEN)))]
        continued = recovered.call("stage-frame", request, now=413)
        assert continued["durable"] is True and continued["tx"] == []
        assert recovered.call("resume", continued["resume"], now=413)["tx"] == expected
    finally:
        recovered.close()


def race(command, directory):
    database = directory / "race.sqlite"
    first = Mode(command, database)
    second = None
    try:
        first.call("boot", {"config": TOPOLOGY})
        second = Mode(command, database)
        with ThreadPoolExecutor(max_workers=2) as executor:
            def allocate(mode, port):
                return mode.call("frame", {"port": "front", "frame": list(tcp_syn(port))})

            created = [executor.submit(allocate, mode, 52111) for mode in (first, second)]
            expected1 = [rows(("back", tcp_backend(40000, BLUE4, BLUE)))]
            assert all(future.result(timeout=5)["tx"] == expected1 for future in created)
            assert first.call("status")["flows"] == 1
            distinct = [executor.submit(allocate, mode, port) for mode, port in ((first, 52112), (second, 52113))]
            records = [future.result(timeout=5) for future in distinct]
            assert all(row["reason"] == "forwarded" for row in records)
            ports = [struct.unpack_from("!H", bytes(row["tx"][0]["frame"]), 38)[0] for row in records]
            assert set(ports) == {40001, 40002}
            assert first.call("status")["flows"] == 3
    finally:
        first.close()
        if second:
            second.close()

def endpoints(command, path):
    bindings = [("front", "f-rx", "f-tx"), ("back", "b-rx", "b-tx")]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as front, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as back:
        for endpoint, (_, _, tx) in zip((front, back), bindings):
            endpoint.bind(str(path / tx))
            endpoint.settimeout(5)
        daemon = [*command, "--db", str(path / "ports.db"), "serve", "--socket", str(path / "admin")]
        for port, rx, tx in bindings:
            daemon.extend(["--virtual", f"{port}={path / rx},{path / tx}"])
        process = subprocess.Popen(daemon)
        try:
            for _ in range(100):
                if all((path / rx).exists() for _, rx, _ in bindings):
                    break
                if process.poll() is not None:
                    raise AssertionError("virtual port daemon stopped")
                time.sleep(0.03)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                control.settimeout(5)
                control.connect(str(path / "admin"))
                control.sendall(json.dumps({"entry": "boot", "arg": {"config": TOPOLOGY}}).encode() + b"\n")
                with control.makefile("rb") as reply:
                    assert json.loads(reply.readline())["reason"] == "configured"
            front.sendto(tcp_syn(50901), str(path / "f-rx"))
            received = back.recv(2048)
            assert received == tcp_backend(40000, BLUE4, BLUE), received
            response = tcp(BLUE4, SNAT4, 8443, 40000, 0x12)
            back.sendto(frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 6, response, back=True), str(path / "b-rx"))
            expected = tcp(VIP4, PC4, 443, 50901, 0x12)
            assert front.recv(2048) == frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, expected, ttl=63)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            assert process.wait(timeout=5) == 0


def admin_call(path, entry, arg=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(5)
        control.connect(str(path / "admin"))
        message = json.dumps({"entry": entry, "arg": arg or {}})
        control.sendall(message.encode() + b"\n")
        with control.makefile("rb") as stream:
            return json.loads(stream.readline())


@contextmanager
def replica_node(command, path, replica=None, *, mode="standby", config=None):
    path.mkdir()
    with ExitStack() as resources:
        front = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        back = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        bindings = [("front", "f-rx", "f-tx"), ("back", "b-rx", "b-tx")]
        daemon = [*command, "--db", str(path / "state"), "serve", "--socket", str(path / "admin")]
        for endpoint, (port, rx, tx) in zip((front, back), bindings):
            endpoint.bind(str(path / tx))
            endpoint.settimeout(5)
            daemon.extend(["--virtual", f"{port}={path / rx},{path / tx}"])
        if replica:
            daemon.extend(["--replica", str(replica / "admin")])
        child = subprocess.Popen(daemon)
        try:
            for _ in range(100):
                if all((path / rx).exists() for _, rx, _ in bindings):
                    break
                assert child.poll() is None
                time.sleep(0.05)
            assert admin_call(path, "boot", {"config": config or TOPOLOGY, "mode": mode})["reason"] == "configured"
            yield child, front, back
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                assert child.wait(timeout=5) == 0


def replica_sockets(command, path):
    active = path / "active"
    standby = path / "standby"
    with replica_node(command, standby) as (_, standby_front, standby_back):
        with replica_node(command, active, standby) as (active_process, front, back):
            assert admin_call(active, "lease", {"epoch": 1, "until": int(time.time()) + 20})["reason"] == "lease-active"
            front.sendto(tcp_syn(51001), str(active / "f-rx"))
            assert back.recv(2048) == tcp_backend(40000, BLUE4, BLUE)
            assert admin_call(standby, "status")["flows"] == 1
            active_process.kill()
            assert active_process.wait(timeout=5) == -signal.SIGKILL
        assert admin_call(standby, "lease", {"epoch": 2, "until": int(time.time()) + 20})["reason"] == "lease-active"
        backend = tcp(BLUE4, SNAT4, 8443, 40000, 0x12)
        standby_back.sendto(frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 6, backend, back=True), str(standby / "b-rx"))
        client = tcp(VIP4, PC4, 443, 51001, 0x12)
        assert standby_front.recv(2048) == frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, client, ttl=63)


def stalled_management_and_storage(command, directory):
    path = directory / "hot"
    with replica_node(command, path, mode="single") as (_, front, back):
        first = tcp_syn(51002)
        front.sendto(first, str(path / "f-rx"))
        assert back.recv(2048) == tcp_backend(40000, BLUE4, BLUE)
        with sqlite3.connect(path / "state", isolation_level=None) as storage:
            storage.execute("BEGIN IMMEDIATE")
            saved = storage.execute("SELECT payload FROM application_state").fetchone()[0]
            assert json.loads(saved)["flows"][0]["allocated"] == 40000
            changed = deepcopy(TOPOLOGY)
            changed["routes"].append({"prefix": list(packed(NET4)), "bits": 32, "out": "back",
                                      "via": list(packed(GREEN4)), "metric": 1})
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as limited:
                    limited.connect(str(path / "admin"))
                    # An incomplete management request remains open throughout forwarding.
                    limited.sendall(b'{"entry":"status"')
                    with ThreadPoolExecutor(max_workers=1) as control:
                        candidate = control.submit(admin_call, path, "candidate", {"generation": 1, "config": changed})
                        try:
                            front.settimeout(1)
                            back.settimeout(1)
                            returning = tcp(BLUE4, SNAT4, 8443, 40000, 0x12)
                            reply = frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 6, returning, back=True)
                            expected = tcp(VIP4, PC4, 443, 51002, 0x12)
                            back.sendto(reply, str(path / "b-rx"))
                            assert front.recv(2048) == frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, expected, ttl=63)
                            ordinary = udp(PC4, NET4, 1234, 4321)
                            front.sendto(frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, ordinary), str(path / "f-rx"))
                            assert back.recv(2048) == frame4(ROUTER_BACK, BLUE, PC4, NET4, 17, ordinary, back=True, ttl=63)
                            live = admin_call(path, "status")
                            assert live["generation"] == 1 and live["counters"]["forwarded"] == 3
                            assert storage.execute("SELECT payload FROM application_state").fetchone()[0] == saved
                            assert not candidate.done()
                        finally:
                            storage.rollback()
                        assert candidate.result(timeout=5)["reason"] == "candidate-validated"
                assert admin_call(path, "commit") == {"reason": "committed", "generation": 2}
                front.sendto(frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, ordinary), str(path / "f-rx"))
                assert back.recv(2048) == frame4(ROUTER_BACK, GREEN, PC4, NET4, 17, ordinary, back=True, ttl=63)
                assert admin_call(path, "status")["counters"]["forwarded"] == 4
            finally:
                if storage.in_transaction:
                    storage.rollback()
        rejected = subprocess.run([*command, "--db", str(path / "state"), "call", "status"],
                                  capture_output=True, text=True, timeout=5)
        assert rejected.returncode != 0 and "management socket" in rejected.stderr


def stalled_replica(command, directory):
    active, standby, proxy = (directory / name for name in ("active", "standby", "proxy"))
    proxy.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as gate:
        gate.bind(str(proxy / "admin"))
        gate.listen(2)
        gate.settimeout(3)
        with replica_node(command, standby), replica_node(command, active, proxy) as (_, front, back):
            assert admin_call(active, "lease", {"epoch": 1, "until": int(time.time()) + 20})["reason"] == "lease-active"

            def offer():
                conn, _ = gate.accept()
                conn.settimeout(2)
                with conn.makefile("rb") as request:
                    record = json.loads(request.readline())
                assert record["entry"] == "replica"
                return conn, record["arg"]

            def reply(conn, arg):
                response = admin_call(standby, "replica", arg)
                assert response["reason"] == "replica-committed"
                conn.sendall(json.dumps(response).encode() + b"\n")

            front.sendto(tcp_syn(51101), str(active / "f-rx"))
            conn, payload = offer()
            with conn:
                reply(conn, payload)
            assert back.recv(2048) == tcp_backend(40000, BLUE4, BLUE)

            front.sendto(tcp_syn(51102), str(active / "f-rx"))
            conn, payload = offer()
            with conn:
                # The second acknowledgment is gated by the successful established frame.
                front.settimeout(1)
                returning = tcp(BLUE4, SNAT4, 8443, 40000, 0x12)
                back.sendto(frame4(BLUE, ROUTER_BACK, BLUE4, SNAT4, 6, returning, back=True), str(active / "b-rx"))
                expected = tcp(VIP4, PC4, 443, 51101, 0x12)
                assert front.recv(2048) == frame4(ROUTER_FRONT, CLIENT, VIP4, PC4, 6, expected, ttl=63)
                assert admin_call(active, "status")["flows"] == 1
                assert admin_call(standby, "status")["flows"] == 1
                back.setblocking(False)
                try:
                    back.recv(2048)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("first packet escaped before the standby acknowledgment")
                finally:
                    back.settimeout(5)
                reply(conn, payload)
            assert back.recv(2048) == tcp_backend(40001, GREEN4, GREEN)
            assert admin_call(active, "status")["flows"] == 2
            assert admin_call(standby, "status")["flows"] == 2


def peer_socket(command, directory):
    path = directory / "peer"
    with replica_node(command, path, mode="single"):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(5)
            endpoint = f"127.0.0.1,{server.getsockname()[1]}"
            peer = subprocess.Popen([*command, "peer", "peer-a", endpoint, "--socket", str(path / "admin")])
            try:
                connection, _ = server.accept()
                connection.settimeout(3)
                with connection, connection.makefile("rb") as stream:
                    def receive():
                        header = stream.read(19)
                        assert header.startswith(bytes([255]) * 16), header
                        length, kind = struct.unpack_from("!HB", header, 16)
                        return kind, stream.read(length - 19)

                    assert receive()[0] == 1
                    body = struct.pack("!BHH4sB", 4, 64513, 30, packed(BLUE4), 0)
                    message = bgp_message(1, body)
                    connection.sendall(message[:12])
                    connection.sendall(message[12:] + bgp_message(4))
                    assert receive() == (4, b"")
                    assert receive()[0] == 2
                    attrs = bytes([64, 1, 1, 0, 64, 2, 4, 2, 1]) + struct.pack("!H", 64513) + bytes([64, 3, 4]) + packed(BLUE4)
                    prefix = bytes([24, 203, 0, 114])
                    connection.sendall(bgp_message(2, struct.pack("!HH", 0, len(attrs)) + attrs + prefix))
                    for _ in range(50):
                        if admin_call(path, "status")["peer-routes"] == 1:
                            break
                        time.sleep(0.03)
                    assert admin_call(path, "status")["peer-routes"] == 1
                assert peer.wait(timeout=5) == 0
                assert admin_call(path, "status")["peer-routes"] == 0
            finally:
                if peer.poll() is None:
                    peer.kill()
                    peer.wait(timeout=5)

def loopback_port():
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


def daemon_neighbor(gobgp, command, directory):
    port, api = loopback_port(), loopback_port()
    while port == api:
        api = loopback_port()
    config = directory / "gobgp.toml"
    config.write_text(f"""
[global.config]
as = 64513
router-id = "198.51.100.20"
port = {port}
local-address-list = ["127.0.0.1"]

[[neighbors]]
[neighbors.config]
neighbor-address = "127.0.0.1"
peer-as = 64512
[neighbors.transport.config]
passive-mode = true
local-address = "127.0.0.1"
[[neighbors.afi-safis]]
[neighbors.afi-safis.config]
afi-safi-name = "ipv4-unicast"
""")
    base = [str(gobgp / "gobgp"), "--host", "127.0.0.1", "--port", str(api)]

    def cli(*args):
        return subprocess.run([*base, *args], capture_output=True, text=True, timeout=3)

    def wait_for(probe, name):
        for _ in range(80):
            if probe():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for " + name)

    database = directory / "external-peer.sqlite"
    mode = Mode(command, database)
    peer = None
    with (directory / "gobgp.log").open("w+") as log:
        daemon = subprocess.Popen(
            [str(gobgp / "gobgpd"), "-f", str(config), "--api-hosts", f"127.0.0.1:{api}"],
            stdout=log, stderr=log,
        )
        try:
            wait_for(lambda: cli("neighbor").returncode == 0, "GoBGP startup")
            topology = deepcopy(TOPOLOGY)
            topology["peers"][0]["address"] = list(packed("127.0.0.1"))
            topology["neighbors"].append({
                "port": "back", "address": list(packed("127.0.0.1")),
                "mac": [2, 0, 0, 0, 0, 20], "until": 9223372036854775807,
            })
            assert mode.call("boot", {"config": topology})["reason"] == "configured"
            peer = subprocess.Popen([*command, "--db", str(database), "peer", "peer-a", f"127.0.0.1,{port}"])
            wait_for(lambda: "192.0.2.0/24" in cli("global", "rib").stdout, "static export in GoBGP")
            result = cli("global", "rib", "add", "203.0.114.0/24", "nexthop", "127.0.0.1")
            assert result.returncode == 0, result.stderr
            wait_for(lambda: mode.call("status")["peer-routes"] == 1, "GoBGP import")
            target = "203.0.114.60"
            segment = udp(PC4, target, 12345, 53)
            request = list(frame4(CLIENT, ROUTER_FRONT, PC4, target, 17, segment))
            out = mode.call("frame", {"port": "front", "frame": request})
            expected = frame4(ROUTER_BACK, BLUE, PC4, target, 17, segment, back=True, ttl=63)
            assert out["tx"] == [rows(("back", expected))]
            result = cli("global", "rib", "del", "203.0.114.0/24")
            assert result.returncode == 0, result.stderr
            wait_for(lambda: mode.call("status")["peer-routes"] == 0, "GoBGP withdrawal")
            assert mode.call("frame", {"port": "front", "frame": request})["reason"] == "no-route"
        except BaseException:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if peer and peer.poll() is None:
                peer.send_signal(signal.SIGINT)
                peer.wait(timeout=5)
            daemon.terminate()
            daemon.wait(timeout=5)
            mode.close()


def health_socket(command, directory):
    path = directory / "health"
    with socket.socket() as backend:
        backend.bind(("127.0.0.1", 0))
        backend.listen(1)
        backend.settimeout(3)
        config = deepcopy(TOPOLOGY)
        config["listeners"][0]["backends"][0].update(
            address=list(packed("127.0.0.1")), healthy=False, **{"probe-port": backend.getsockname()[1]},
        )
        with replica_node(command, path, mode="single", config=config):
            invocation = [*command, "probe", "--socket", str(path / "admin")]
            first = json.loads(subprocess.check_output(invocation, timeout=5))
            assert first[0]["reason"] == "backend-updated"
            connection, _ = backend.accept()
            connection.close()
            backend.close()
            second = json.loads(subprocess.check_output(invocation, timeout=5))
            assert second[0]["reason"] == "backend-updated"
            assert second[0]["generation"] == first[0]["generation"] + 1


def mutations(compiler, baseline, baseline_ownership, directory):
    modified = directory / "source"
    modified.mkdir()
    manifest = json.loads((HERE / "manifest.json").read_text())
    for name in (*manifest["sources"], *manifest["assets"], "manifest.json"):
        contents = (HERE / name).read_text()
        if name == "appliance.shen":
            assert contents.count('(rt.frame "round-robin" Defer Arg)') == 1
            assert contents.count('Interface Packet -> (rt.policy-at (get Interface "policy") Packet 0)') == 1
            contents = contents.replace('(rt.frame "round-robin" Defer Arg)', '(rt.frame "reverse" Defer Arg)')
            contents = contents.replace(
                'Interface Packet -> (rt.policy-at (get Interface "policy") Packet 0)',
                'Interface Packet -> (and (not (= (get Packet "dst") [203 0 113 89])) (rt.policy-at (get Interface "policy") Packet 0))',
            )
        (modified / name).write_text(contents)
    release = directory / "alternate"
    subprocess.run([sys.executable, str(compiler), "build", str(modified / "manifest.json"), "--out", str(release)],
                   check=True, timeout=150)
    ownership = json.loads((release / "ownership.json").read_text())
    for part in ("compiler", "compiler_dependencies", "drivers", "assets", "manifest"):
        assert ownership["digests"].get(part) == baseline_ownership["digests"].get(part), part
    choices = ownership["definitions"]["rt.choose-index"]["residual"]
    assert choices and all(choice["static"][0] == {"value": "reverse"} for choice in choices)
    changed = [sys.executable, str(release / "router-nlb")]
    db = directory / "persistent.sqlite"
    base = Mode(baseline, db)
    try:
        base.call("boot", {"config": TOPOLOGY})
        original = base.call("frame", {"port": "front", "frame": list(tcp_syn(41111))})
        assert original["tx"] == [rows(("back", tcp_backend(40000, BLUE4, BLUE)))]
    finally:
        base.close()
    upgraded = Mode(changed, db)
    try:
        original = upgraded.call("frame", {"port": "front", "frame": list(tcp_syn(41111))})
        assert original["tx"] == [rows(("back", tcp_backend(40000, BLUE4, BLUE)))]
        drained = {"listener": ["main", 6, list(packed(VIP4)), 443], "backend": "blue", "value": True}
        # Reverse starts from green at serial zero. A second database exercises this without carried state.
        unchanged = upgraded.call("frame", {"port": "front", "frame": []})
        assert unchanged["reason"] == "truncated-ethernet"
        routed = frame4(CLIENT, ROUTER_FRONT, PC4, NET4, 17, udp(PC4, NET4, 20101, 91))
        assert upgraded.call("frame", {"port": "front", "frame": list(routed)})["reason"] == "ingress-policy"
        assert upgraded.call("drain", drained)["reason"] == "backend-updated"
    finally:
        upgraded.close()
    fresh = Mode(changed, directory / "fresh.sqlite")
    try:
        fresh.call("boot", {"config": TOPOLOGY})
        reversed_choice = fresh.call("frame", {"port": "front", "frame": list(tcp_syn(41112))})
        assert reversed_choice["tx"] == [rows(("back", tcp_backend(40000, GREEN4, GREEN)))]
    finally:
        fresh.close()
    rolled_back = Mode(baseline, db)
    try:
        original = rolled_back.call("frame", {"port": "front", "frame": list(tcp_syn(41111))})
        assert original["tx"] == [rows(("back", tcp_backend(40000, BLUE4, BLUE)))]
    finally:
        rolled_back.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--compiler", type=Path, help="also rebuild a source-only mutation")
    parser.add_argument("--gobgp", type=Path, help="also qualify against GoBGP binaries in the given directory")
    parser.add_argument("--replay-only", action="store_true", help="exercise executable replays without sockets")
    args = parser.parse_args()
    if args.replay_only and args.gobgp:
        parser.error("--gobgp requires sockets")
    native = [sys.executable, str(args.release.resolve() / "router-nlb")]
    ownership = json.loads((args.release / "ownership.json").read_text())
    assert all(choice["static"][0] == {"value": "round-robin"}
               for choice in ownership["definitions"]["rt.choose-index"]["residual"])
    reference = [sys.executable, str(HERE / "driver.py"), "--reference", str(HERE / "manifest.json")]
    counts = 0
    for test in (routing_replay, neighbor_replay, flow_replay):
        with TemporaryDirectory(prefix="router-", dir="/tmp") as temp:
            pair = Pair(reference, native, Path(temp))
            try:
                test(pair)
                counts += pair.calls
            finally:
                pair.close()
    with TemporaryDirectory(prefix="router-", dir="/tmp") as temp:
        path = Path(temp)
        (path / "active").mkdir()
        (path / "standby").mkdir()
        counts += availability(reference, native, path)
    for command in (reference, native):
        with TemporaryDirectory(prefix="router-", dir="/tmp") as temp:
            race(command, Path(temp))
            durable_reservations(command, Path(temp))
            if not args.replay_only:
                endpoints(command, Path(temp))
                peer_socket(command, Path(temp))
                replica_sockets(command, Path(temp))
                health_socket(command, Path(temp))
                with TemporaryDirectory(prefix="router-hot-", dir="/tmp") as isolated:
                    stalled_management_and_storage(command, Path(isolated))
                with TemporaryDirectory(prefix="router-ha-", dir="/tmp") as isolated:
                    stalled_replica(command, Path(isolated))
                if args.gobgp:
                    daemon_neighbor(args.gobgp.resolve(), command, Path(temp))
    print(f"{counts} reference/native event pairs plus concurrent allocation and crash-safe deadline reservations in both modes")
    if not args.replay_only:
        print("Unix Ethernet ports, replica failover, TCP peer replay, TCP health checks, and held management/storage/replication passed in both modes")
    if args.gobgp:
        print("GoBGP establishment, static export, import, forwarding, and withdrawal passed in both modes")
    if args.compiler:
        with TemporaryDirectory(prefix="router-", dir="/tmp") as temp:
            mutations(args.compiler.resolve(), native, ownership, Path(temp))
        print("source-only forwarding and selection mutations and persistent-flow upgrade and rollback passed")


if __name__ == "__main__":
    main()
