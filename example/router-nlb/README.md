# Generated router and layer-4 load balancer

The application definitions are in [appliance.shen](appliance.shen) and [bgp.shen](bgp.shen). The shared application evaluator interprets those definitions and specializes the manifest's entry points to a native extension. It produces `ownership.json` with source locations, residual functions, capabilities, and release digests. The Python host supplies SQLite transactions and Ethernet, TCP, Unix socket, and clock access. The source decides what packets to send, which routes and backends to use, and when a replicated flow may transmit.

## Build and qualify

Use the Python environment with Cython and a C compiler used for the shared application compiler. Run these commands from the repository root.

```sh
build_root=$(mktemp -d)
python3 example/webapp/appc.py build example/router-nlb/manifest.json --out "$build_root/release"
python3 example/webapp/appc.py inspect "$build_root/release"
python3 example/router-nlb/check.py "$build_root/release" --compiler example/webapp/appc.py
```

The check drives an independent packet encoder against both modes. It compares frame bytes, egress, counters and generations, exercises persistent state across processes, and starts separate Unix datagram endpoints and an independent BGP wire fixture over TCP. With `--compiler`, it rebuilds with only the forwarding and pool-selection definitions edited, then opens the same database under both builds. `--replay-only` runs the process, storage, and mutation checks in a sandbox without sockets.

For an independent BGP daemon check, install the pinned test peer into a temporary directory and pass it to the same harness.

```sh
peer_tools=$(mktemp -d)
GOBIN="$peer_tools" go install github.com/osrg/gobgp/v3/cmd/gobgpd@v3.37.0 github.com/osrg/gobgp/v3/cmd/gobgp@v3.37.0
python3 example/router-nlb/check.py "$build_root/release" --gobgp "$peer_tools"
```

The test checks learned forwarding bytes and live withdrawal as well as the appliance's static export. [GoBGP v3 configuration](https://github.com/osrg/gobgp/blob/v3.37.0/docs/sources/configuration.md).

The release's Python ABI and architecture are recorded in `ownership.json`. Build on a matching Linux target for Linux installation. Run the reference service by substituting `python3 example/router-nlb/driver.py --reference example/router-nlb/manifest.json` for `"$build_root/release/router-nlb"`.

## Operate a local topology

[The fixture](topology.json) defines a client port, a VLAN 42 backend port, static routes, fixed neighbor entries, and two listeners. The test harness opens both endpoints automatically. For a separate local process, point `--virtual` at private paths and supply a bound datagram socket at each `*-tx` endpoint.

```sh
run_root=$(mktemp -d /tmp/router.XXXXXX)
python3 -c 'import json,sys; print(json.dumps({"config":json.load(open(sys.argv[1]))}))' \
  example/router-nlb/topology.json > "$run_root/boot.json"
"$build_root/release/router-nlb" --db "$run_root/router.db" call boot "$run_root/boot.json"
"$build_root/release/router-nlb" --db "$run_root/router.db" serve \
  --socket "$run_root/admin" \
  --virtual "front=$run_root/front-rx,$run_root/front-tx" \
  --virtual "back=$run_root/back-rx,$run_root/back-tx"
```

Use the same paths in a second shell.

```sh
"$build_root/release/router-nlb" call status --socket "$run_root/admin"
```

The socket and its parent directory restrict access through Unix ownership. The management API accepts one JSON object with `entry` and `arg` per connection. Its operation map is in the [manifest](manifest.json). Use `candidate` with `{"generation":N,"config":{…}}`, followed by `commit`. A generation change invalidates the candidate. `rollback` takes the current generation and republishes the previous configuration. Configuration validation checks cross-references and structural bounds in addition to the [configuration schema](config.schema.json). Persisted states use schema version 1. The release digest identifies the build responsible for each stored state revision. A running packet service owns its database. Use the Unix socket for live operations and reserve direct `--db` access for bootstrapping and offline replay.

To drain a member, submit `drain` with `{"listener":["main",6,[203,0,113,80],443],"backend":"blue","value":true}`. Existing bindings retain their addresses and ports. An optional `probe-port` enables bounded TCP connect checks on a backend, including one serving UDP traffic. `probe --socket "$run_root/admin"` performs a scan and the generated health transition applies only to the still-matching target. A scan attempts up to 32 endpoints. A TCP connection is the probe's health signal.

The Linux `--physical LABEL=DEVICE` adapter binds AF_PACKET receive and transmit on named dedicated Ethernet devices. The supplied systemd units use `/etc/router-nlb/ports` with a `ROUTER_PORTS` value such as `"--physical front=enp1s0 --physical back=enp2s0"`. Provision the service user and boot configuration before starting the unit. Keep administrative access on a separate path. Device setup and kernel ownership must be handled by the installer outside the generated process. The driver rejects non-Ethernet devices and ports enslaved to a bridge or bond.

## Packet and routing profile

| Path | Behavior |
| --- | --- |
| Ethernet | Whole software frames, with no FCS. Untagged or one 802.1Q tag per port. Frame cap is 9,218 bytes. |
| IPv4 | Fixed 20-byte header. Verify and update the checksum. Discard options, fragments, and invalid lengths. |
| IPv6 | Base header forwarding. Discard extension chains in the configured profile, including fragment headers. |
| IP routing | Longest prefix in the ingress tenant. Lower metric wins among equal prefix lengths. Apply ordered ingress ACL rules, update TTL or hop limit, and resolve the egress next hop. |
| ARP and NDP | Resolve interface and configured listener/translation addresses. Learn validated link addresses and expire them after 60 seconds. Queue a bounded number of frames for up to three seconds. This NDP profile handles single link-layer-address options for NS/NA. |
| MTU and ICMP | Generate IPv4 fragmentation-needed or IPv6 packet-too-big, time-exceeded, and no-route replies where allowed. Arrival-side gateway errors quote the original network packet. Drop ICMP errors addressed to a translation alias with `translated-icmp-unsupported`. Forwarding does not reassemble fragments. |
| TCP and UDP | Validate lengths and transport checksum. Preserve IPv4 UDP zero checksums. Recompute both directional checksums on same-family address and port translation. TCP opens a binding on an initial SYN. |
| Offload | Complete software Ethernet frame and checksum profile. Hardware VLAN stripping, checksum offload metadata, RSS queue migration, GSO and jumbo scatter/gather need a separately qualified driver profile. |

The packet service holds the published state in memory. A frame that routes without acquiring new ownership or extending a durable reservation reads that state and records its result without waiting on SQLite, management clients, or the standby. The durable worker commits configuration and ownership changes before publishing them. Status reads the same live state as the dataplane. The packet service coalesces telemetry and idle refreshes in asynchronous checkpoints at one-second intervals when storage is available. Packet batches hold one generation. One flow record holds both directional identities and allocator ownership. Per-tenant and global limits cap live bindings. Port exhaustion leaves an existing binding intact and returns `port-exhausted`. TCP's sliding idle limit in single mode is 300 seconds. UDP uses 30 seconds. A tuple is eligible for a new choice at its expiry boundary. The control response budget allows up to 32 emissions per clock second across all ports. `status` reports ingress dispositions, including drops and frames queued for discovery. A queued frame's eventual delivery is part of the neighbor event and does not increment the original ingress disposition a second time.

The [BGP-4](https://www.rfc-editor.org/rfc/rfc4271) profile is classic IPv4 unicast eBGP with explicit 16-bit ASNs, peer address and port, exact-prefix import/export allowlists, and no transit re-advertisement. Advertised next hop must equal the configured peer IPv4 address. Local static routes eligible for export are sent on establishment. Learned routes are withdrawn by UPDATE, TCP closure, or hold expiration. The OPEN parser accepts well-formed capabilities without activating additional address families. An unrecognized OPEN optional parameter produces an OPEN error notification. Invalid framing, attributes, or message state use the defined notification paths in the source. Ethernet discovery and IPv6 forwarding follow the restricted profiles above. [IPv6 specification](https://www.rfc-editor.org/rfc/rfc8200), [IPv6 Neighbor Discovery](https://www.rfc-editor.org/rfc/rfc4861).

Use `peer ID HOST,PORT --socket "$run_root/admin"` for the outbound TCP transport while serving. In the virtual test the independent peer listens on loopback. On a configured link, schedule the command under service supervision and assign the management routing needed to reach the peer.

## Availability and recovery

The virtual profile can start two independently persisted standby instances. `lease` accepts a trusted epoch and absolute expiration, at most 30 seconds in the future. A virtual active can name the standby's Unix socket with `--replica`. The active holds a first frame while the standby stores the record, including reverse identity, allocator ownership, and absolute expiration. Only the matching acknowledgment releases the first frame through the active's subsequent commit. Retransmission of the same outstanding offer reuses its ID and the standby's latest matching acknowledgment. An abandoned offer can reserve a port on the standby until expiry. Established replicated flows keep the same absolute expiration on both nodes and may forward during replication loss while the local lease is valid. The packet service rechecks that lease at transmission, including after a delayed acknowledgment or durable write.

Single-node flows persist a port reservation covering the idle deadline plus 30 seconds before the first frame. A refresh reaching beyond that reservation commits its extension before transmitting the triggering frame. The service can keep other established flows moving while that write is pending. When a process restarts, it keeps the original binding through the later of the saved idle deadline and reservation, so a missed checkpoint cannot allow early port reuse. Reserving for recovery can retain an otherwise idle flow longer after a crash.

At packet-service startup, the generated `recover` entry runs before the host opens ports. It clears volatile peer, pending-neighbor, and leadership state and retains committed configuration and flow records. Resume by rebuilding peer sessions and acquiring a higher epoch. When replacing a standby, restore its SQLite state from a consistent backup, start it as standby, and verify configuration compatibility before accepting offers. For a same-schema upgrade or failback, retain the old release and database backup, recover volatile state, verify status and neighbor discovery, and reauthorize leadership independently. A rejected candidate leaves the previous committed version active and can be resubmitted through the separate management socket.

## Qualification boundary

The checked environment is a local virtual dataplane with a native extension. Both modes qualified under a held SQLite write lock, a client that paused mid-request, and a standby acknowledgment held until an established frame was delivered. Linux physical-device setup, systemd installation, physical-peer interoperability, a production quorum lease and switch fencing integration, cross-host replication, AF_XDP and hardware queue ownership, and capacity or disruption measurements on named equipment remain release gates. The Unix replica adapter and injected lease outcomes exercise the local acknowledgment protocol. Deploying an availability pair requires a lease provider that prevents simultaneous owners and restores the committed replication boundary after failure.
