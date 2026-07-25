# Scripts

Local diagnostic and validation entry points default to read-only or dry-run
behavior, fixed argument vectors, and bounded execution/input/output/history.
Their `--help` text is the option-level source of truth.

Hardware claims require later execution on the authorized Pi; locally generated
fixture output is not capability evidence. Any future mutating tool must state
its separate authorization gate and refuse until exact identity/confirmation
invariants pass. See
[`docs/test-procedures.md`](../docs/test-procedures.md).
