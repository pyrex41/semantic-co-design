"""Run Maude's bounded safety search and the intentionally faulty mutant."""
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).parent

def run(module):
    maude = os.environ.get("MAUDE", "maude")
    proc = subprocess.run([maude, "-no-banner", "-no-advise", "-batch", str(ROOT / module)],
                          text=True, capture_output=True, check=True, timeout=120)
    return proc.stdout

if __name__ == "__main__":
    safe = run("railway-explore.maude")
    mutant = run("railway-mutant.maude")
    assert "No solution" in safe and "states: 5" in safe and "rewrites: 8" in safe
    assert "Solution 1" in mutant and "states: 4" in mutant and "rewrites: 3" in mutant
    print("maude exploration passed")
