# Streaming decoder

The grammar fixture contains the magic byte, a list of accepted kinds, the length byte order (0 big-endian, 1 little-endian), the maximum payload length, and the checksum seed. A frame contains magic, kind, two length bytes, the payload and a checksum byte. The checksum adds the seed, kind, length bytes and payload modulo 256.

A runtime row contains successive chunks, each prefixed with its byte count. One byte loop retains the parser phase and buffered payload across chunk boundaries. A complete message emits `-1 kind length payload…`. Each chunk emits `-2 consumed errorOffset phase remainingPayload`. The error offset is zero-based, or -1 for a valid prefix. The consumed count includes the offending byte. After the first invalid byte the parser freezes and later chunk boundaries report the frozen state.

Phases 0–5 expect magic, kind, first length byte, second length byte, payload and checksum, respectively. Remaining payload is meaningful in phase 4 and otherwise zero. Chunk lengths must describe the supplied bytes exactly.

Run `python3 -m examples.decoder.check` inside the Nix shell. It compares every chunk observation with the independent decoder, including empty chunks, every two-chunk split of representative valid and invalid streams, byte-at-a-time delivery, and a changed little-endian grammar.
