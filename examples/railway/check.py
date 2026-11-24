import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from specializer import ROOT
from specializer.replay import verify
from .railway import replay

HERE = Path(__file__).parent
MUTATIONS = {
    "shen": ("[and [eq [var 0] Route] [eq [var 1] 0]]\n        [seq [set 0 0]",
             "[eq [var 0] Route]\n        [seq [set 0 0]"),
    "maude": ("box(and box(eq box(var 0) Route) box(eq box(var 1) 0))\n        box(seq box(set 0 0)",
              "box(eq box(var 0) Route)\n        box(seq box(set 0 0)")}


def explore(spec):
    cmd = [os.environ.get("MAUDE","maude"), "-no-advise","-no-banner","-batch",
           ROOT / "stage.maude",spec,HERE / "explore.maude"]
    search = "search [,6] cfg(0,0,0) =>* cfg(R:T, O:T, S:T) .\n"
    def maude(request):
        p = subprocess.run(cmd,input=request+"quit\n",capture_output=True,text=True,check=True,timeout=30)
        assert not p.stderr, p.stderr
        return p.stdout
    results = re.findall(r"Solution \d+ \(state (\d+)\).*?R --> (\d+)\s+O:T --> (\d+)\s+S:T --> (\d+)",
                         maude(search),re.S)
    assert results
    states = {}
    for sid, reservation, train, switch in results:
        output = maude(search + f"show path labels {sid} .\n")
        path = list(map(int,re.findall(r"^event-(\d+)$",output,re.M)))
        states[tuple(map(int,(reservation,train,switch)))] = path
    return states


def main():
    states = explore(HERE / "spec.maude")
    assert len(states) == 7
    assert all(not train or train == lock == switch for lock,train,switch in states)
    rows = [prefix + [event] for prefix in states.values() for event in range(8)]
    rows.extend([[],[0,1,4],[0,2,1,4,3,4]])
    verify("railway",[1,2],rows,[replay(row) for row in rows])
    with TemporaryDirectory() as tmp:
        spec = Path(tmp) / "spec.maude"
        old,new = MUTATIONS["maude"]
        source = (HERE / spec.name).read_text()
        assert source.count(old) == 1
        spec.write_text(source.replace(old,new))
        mutants = explore(spec)
    witnesses = [path for (_,train,switch),path in mutants.items() if train and train != switch]
    witness = min(witnesses,key=len)
    assert len(witness) == 4
    verify("railway",[1,2],[witness],[replay(witness,faulty=True)],mutations=MUTATIONS)
    print(f"Railway depth 6: {len(states)} safe states, {len(states)*8} state/event transitions; "
          f"{len(mutants)} mutant states, shortest conflicting grant {witness}.")


if __name__ == "__main__":
    main()
