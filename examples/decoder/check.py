import json
from pathlib import Path

from .decoder import frame, decode
from specializer.replay import verify

ROOT = Path(__file__).parent


def encoded(chunks):
    return [v for chunk in chunks for v in [len(chunk), *chunk]]


def main():
    grammar = json.loads((ROOT / "grammar.json").read_text())
    packet = frame(grammar, 1, []) + frame(grammar, 2, [0,165,255,3])
    streams = [packet, [0], [165,99], [165,1,1], [165,1,0,33], packet[:-1]+[0]]
    rows = [[],[0]]
    for stream in streams:
        rows.extend(encoded([stream[:cut],stream[cut:]]) for cut in range(len(stream)+1))
        rows.append(encoded([[byte] for byte in stream]))
    assert decode(grammar, encoded([packet])) == [-1,1,0,-1,2,4,0,165,255,3,-2,14,-1,0,0]
    verify("decoder",grammar,rows,[decode(grammar,r) for r in rows])
    alternate = [171,[3],1,260,17]
    chunks = [frame(alternate,3,list(range(256)))[:8],frame(alternate,3,list(range(256)))[8:]]
    row = encoded(chunks)
    verify("decoder",alternate,[row],[decode(alternate,row)])
    row = encoded([frame(grammar,1,[])])
    verify("decoder",alternate,[row],[decode(alternate,row)])
    # Source-only mutation changes the checksum initialization.
    wrong = [*grammar[:-1], grammar[-1]+1]
    verify("decoder",grammar,[row],[decode(wrong,row)], mutations={
        "shen": ("[add Seed [var 9]]","[add [add Seed 1] [var 9]]"),
        "maude": ("box(add Seed box(var 9))","box(add box(add Seed 1) box(var 9))")})
    print(f"Decoder: {len(rows)} chunk schedules, changed grammar and checksum mutant passed.")


if __name__ == "__main__":
    main()
