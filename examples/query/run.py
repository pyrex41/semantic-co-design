import json
from pathlib import Path
from query import QUERIES, execute, fixture


def main():
    cutoff = 12
    result = {name: execute(ast, fixture(), cutoff) for name, ast in QUERIES.items()}
    print(json.dumps({"cutoff": cutoff, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
