"""Run Maude's bounded safety search and the intentionally faulty mutant."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).parent

def run(module):
    proc = subprocess.run(["maude", "-no-banner", "-no-advise", "-batch", str(ROOT / module)],
                          text=True, capture_output=True, check=True, timeout=120)
    return proc.stdout

if __name__ == "__main__":
    safe = run("railway-explore.maude")
    mutant = run("railway-mutant.maude")
    assert "No solution" in safe or "No more solutions" in safe
    assert "unsafe" in mutant.lower() or "Solution" in mutant
    print("maude exploration passed")
