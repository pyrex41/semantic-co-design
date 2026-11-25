# Run a book pattern in Shen and Maude, then specialize it

Give the review-queue program a live amount of 125. The program tries the 100-unit handler before the 10-unit handler and returns queue 2. Reverse the order in the program and it returns queue 1 for the same amount. Both interpreters execute that edit. Each specializer also turns the edited program into a new function you can compile and call on fresh amounts.

The [inventory](inventory.json) covers 75 catalog families. Each entry has a program, a meaningful source edit, static configuration, an input contract and expected observations. The Python files construct the syntax trees and describe their tests. You can export those trees as named programs and run or edit them directly in either language.

| Book | Cases | Where the program and bounds live |
|---|---:|---|
| Gang of Four | 23 | [Creational and structural](cases/gof_objects.py) · [Behavioral](cases/gof_behavior.py) |
| SICP | 32 | [Procedures and data](cases/sicp_data.py) · [State and streams](cases/sicp_state.py) · [Languages and machines](cases/sicp_languages.py) |
| PAIP | 20 | [Rules, algebra and language compilation](cases/paip_core.py) · [Grammar, logic, search and constraints](cases/paip_search.py) |

## Inspect the two residuals

From the repository root, enter `nix develop` and run:

```sh
work=$(mktemp -d)
python3 -m experiments.patterns.projections.check \
  --case gof-chain-of-responsibility --emit "$work/review"
ls "$work/review"
```

Open `evidence.json` to see the source and residual trees plus all four observed outputs for every input. `shen.c` and `maude.c` are the native functions lowered from the two residuals. `replay.json` records the commands for rerunning the interpreters, compiler and native functions from the evidence directory. Omit `--case` to exercise the exact inventory and write all the batches.

## Use a pattern from either language

```sh
python3 -m experiments.patterns.projections.library \
  --case gof-chain-of-responsibility --out "$work/lib"
```

This writes self-contained `patterns.shen` and `patterns.maude` modules. Omit `--case` to export every family. Each module exposes three names per family ending in `-program`, `-edit` and `-config`. In Shen load the module, then evaluate:

```shen
(let P (gof-chain-of-responsibility-program)
     C (gof-chain-of-responsibility-config)
     R (proj-pe P C)
     [(proj-run P C [125]) (proj-run R [] [125])])
```

Both readings give 2. With `gof-chain-of-responsibility-edit` both give 1. In Maude, load the module and run:

```maude
red in BOOK-PATTERNS : eval(gof-chain-of-responsibility-program,
  gof-chain-of-responsibility-config, cons(125, nil)) .
red in BOOK-PATTERNS : eval(specialize(gof-chain-of-responsibility-edit,
  gof-chain-of-responsibility-config), nil, cons(125, nil)) .
```

## What the first projection checks

For a pattern program `P`, build-time configuration `C`, and fresh input `x`, each language computes `R = specialize(P, C)`. The required law is `interpret(P, C, x) = native(R, x)`. The test changes `P`, holds `C` and the specializers fixed, and requires at least one input to distinguish the two programs. A residual must eliminate the configuration reads and differ structurally from its source. Each language independently produces its residual. Both use the same small C backend. [Shen interpreter and specializer](core.shen) · [Maude interpreter and specializer](core.maude) · [Native lowering](native.py)

The object language has integer arithmetic, indexed inputs, lexical bindings, conditionals and bounded folds. The case contract specifies how it represents its domain. For example, an object profile can expose stable handles and observed calls, and a stream profile can expose previously forced cells and the force count. This gives a finite observation of each family; the [original catalog](../README.md) supplies the richer libraries and host APIs. The native path uses checked signed 64-bit integers and the cases use values exactly representable across all three runtimes. Second- and third-projection self-application and performance comparisons are separate experiments.

## Qualification

```sh
nix build --no-link -L '.#checks.aarch64-darwin.book-projections'
```

Use the matching Nix system name on Linux. The gate checks all 75 original and edited programs through the four paths, exercises generated trees against an independent oracle, compares at least one corresponding observation from every family with its original catalog library, and loads both exported language modules to execute all 150 named program variants. The catalog comparison covers the observations each original API exposes. For example, GoF Singleton checks repeated getter identity, and the PAIP game adapter derives its leaf scores from actual two-ply Othello positions. Some source edits deliberately change behavior the original API cannot express. Those edits still go through both interpreters and both native paths. Negative controls reject incorrect outputs, unchanged or malformed residuals, and surviving static configuration reads. [Four-path gate](check.py) · [Generated-tree tests](check_core.py) · [Catalog parity](check_catalog.py) · [Export tests](check_library.py)
