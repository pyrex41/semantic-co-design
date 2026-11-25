"""Run the starter C client and each book's independent acceptance suite."""
import subprocess
import sys


def main():
    for suite in ("check", "gof.check", "sicp.check", "paip.check", "workflow"):
        module = "experiments.patterns." + suite
        print("+", sys.executable, "-m", module, flush=True)
        subprocess.run([sys.executable, "-m", module], check=True, timeout=360)


if __name__ == "__main__":
    main()
