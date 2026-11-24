# Bytecode VM

The fixed input is `[seed, emptyPolicy, instructions]`. Each iteration starts with the accumulator on the operand stack. The instructions push the current array element (`[0]`), push a constant (`[1,n]`), add (`[2]`), multiply (`[3]`), or take the unsigned maximum (`[4]`). A valid body leaves one word. Arithmetic wraps at 32 bits. Inputs and outputs represent unsigned words, and signed inputs are reduced modulo 2³².

| Fixture | Accumulator | Empty input |
|---|---|---|
| sum.json | Start at zero and add each value | 0 |
| maximum.json | Seed from the first element and take successive maxima | Empty output line |
| checksum.json | Start at 17, multiply by 31, add each value | 17 |

Each specification walks the bytecode at build time. Its operand stack contains residual expressions. The native binary retains the array loop and the resulting accumulator expression. Run `python3 -m examples.vm.check` in the Nix shell to compare the direct stack-machine oracle with both source interpreters and binaries, then rebuild after separate instruction and arithmetic-rule mutations.
