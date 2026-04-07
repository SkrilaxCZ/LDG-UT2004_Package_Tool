# Usage guide

This document is the full command reference for **UT2004PackageUtil**. For an
overview and installation instructions see the [README](../README.md).

## Invocation

```bash
python -m ut2004packageutil <command> [options]
```

Inside the `uv`-managed environment, prefix with `uv run`:

```bash
uv run python -m ut2004packageutil <command> [options]
```

Run any command with `-h`/`--help` to see its options:

```bash
python -m ut2004packageutil decompile --help
```

## Common options

These apply to every command:

| Option                  | Required | Description                                                                 |
|-------------------------|----------|-----------------------------------------------------------------------------|
| `-i`, `--ini`           | yes      | Path to the `UT2004.ini` used to locate dependency packages.                |
| `-b`, `--base-directory`| no       | Extra directory searched for packages **before** the INI's directories.    |
| `-p`, `--package`       | usually  | Path to the input `.u` package.                                             |
| `-o`, `--output`        | usually  | Output path (a `.u` file, or a directory, depending on the command).        |

### How dependencies are resolved

A `.u` package refers to objects in other packages (`Core`, `Engine`, …).
The tool loads those dependencies so references resolve correctly:

- Packages that only contribute **content** (textures, sounds, meshes, …) are
  represented by lightweight placeholders when the real file is not found.
- Packages that contribute **code** (classes, structs, functions, …) must be
  present; the tool searches the `--base-directory` first, then the paths
  configured in `UT2004.ini`.

Point `--ini` at a real `UT2004/System/UT2004.ini` so `Core.u`, `Engine.u`,
and friends can be found.

---

## `info`

Print a detailed dump of a package: flags, GUID, the full name table, the
import table, the export table, and a per-object dump.

```bash
python -m ut2004packageutil info -i <UT2004.ini> -p <package.u>
```

| Option            | Description                     |
|-------------------|---------------------------------|
| `-p`, `--package` | Package to inspect (required).  |

Output goes to stdout; redirect it to a file to keep it:

```bash
python -m ut2004packageutil info -i System/UT2004.ini -p System/MyMod.u > MyMod.txt
```

---

## `xml-export`

Export a package into a directory containing `Package.xml` plus sidecar files
(`UnTextBuffer/` for script text, `UnToken/` for bytecode token streams). Token
streams are written as `.uasm` files: a readable, assembler-style disassembly
(one token per line, nesting by indentation) rather than nested XML. This
is a fully editable representation that can be re-imported with `xml-import`.

```bash
python -m ut2004packageutil xml-export -i <UT2004.ini> -p <package.u> -o <out_dir> [-g]
```

| Option                | Description                                                             |
|-----------------------|-------------------------------------------------------------------------|
| `-p`, `--package`     | Package to export (required).                                           |
| `-o`, `--output`      | Output **directory** (required).                                        |
| `-g`, `--no-generations` | Drop generation history and the GUID (both regenerated on import).   |

---

## `xml-import`

Rebuild a `.u` package from a directory previously produced by `xml-export`.

```bash
python -m ut2004packageutil xml-import -i <UT2004.ini> -d <in_dir> -o <package.u>
```

| Option            | Description                                        |
|-------------------|----------------------------------------------------|
| `-d`, `--input`   | Input **directory** containing `Package.xml`.      |
| `-o`, `--output`  | Output `.u` package file (required).               |

Exporting and re-importing without edits reproduces the original package
byte-for-byte.

---

## `obfuscate`

Rename a package's code symbols to hinder reverse engineering while keeping
the package fully functional. Names that must stay stable are preserved
automatically, including: import references and engine/`Core` names,
config/localized property names, `Exec`/`Event`/operator function names,
state names, native symbols and native-class member variables, and any name
observed as a string in the bytecode.

Every export is marked **private** (the `Public` flag is cleared) so the
obfuscated package doesn't re-export its renamed symbols — in both `--simple`
and the default harder mode.

```bash
python -m ut2004packageutil obfuscate -i <UT2004.ini> -p <package.u> -o <out.u> [-s] [-e <file>] [-m <map.txt>]
```

| Option              | Description                                                                       |
|---------------------|-----------------------------------------------------------------------------------|
| `-p`, `--package`   | Package to obfuscate (required).                                                  |
| `-o`, `--output`    | Output `.u` file (required).                                                      |
| `-s`, `--simple`    | Use *simple* obfuscation (`O<number>` symbols). Default is the *harder* mode.     |
| `-e`, `--exceptions`| Path to a text file of names to preserve (one per line, matched case-insensitively). |
| `-m`, `--map`       | Optional path to write an `obfuscated → original` name map. Omit to skip it.      |

The command prints, per original name, whether it was `Excluding` (preserved,
with the reason) or `Hashing` (rewritten, with the resulting symbol(s)).

### The `-m/--map` reverse map

When `-m` is given, one `<ObfuscatedToken> = <OriginalName>` line is written per
rewritten name. Feed the file straight to [`deobfuscate`](#deobfuscate) to rename
the package back to its original symbols. Simple-mode `O<number>` tokens are
written verbatim; the default harder mode produces non-printable hashes
(newlines and control bytes) that can't sit on a text line, so the token column
is **base64-encoded** and the header carries a `# token-encoding: base64` marker
that `deobfuscate` recognises automatically.

---

## `decompile`

Decompile a package's classes back into `.uc` UnrealScript source, writing one
`<ClassName>.uc` file per class into the output folder. Class declarations,
constants, enums, structs, variables, replication blocks, functions (with
decompiled bodies), states, and `defaultproperties` are all reconstructed.

```bash
python -m ut2004packageutil decompile -i <UT2004.ini> -p <package.u> -o <out_dir> [-s]
```

| Option              | Description                                          |
|---------------------|------------------------------------------------------|
| `-p`, `--package`   | Package to decompile (required).                     |
| `-o`, `--output`    | Output **directory** for the `.uc` files (required). |
| `-s`, `--simplify`  | Enable the clean-up pass (see below).                |

### The `--simplify` clean-up pass

Without `-s`, the output faithfully mirrors the bytecode: every compiler-
inserted cast is explicit, control flow is raw `goto`/label form, and
anti-decompilation junk is left in place. `-s` rewrites that into something
closer to hand-written source. The rewrites are grouped below by what they
touch — expressions, statements/control-flow, and declarations. Each one is
value-preserving: simplified source recompiles to equivalent bytecode.

#### Expression simplifications

- **Redundant casts** — lossless round-trips such as `bool(int(x))` collapse
  to `x`; constant casts fold (`float(false)` → `0.0`, `int(bool(0))` → `0`).
- **Byte→int casts** — the implicit `int(<byte>)` widening the compiler
  inserts is removed (the narrowing `byte(<int>)` is kept).
- **Coerce string casts** — an explicit `string(...)` on an argument bound to
  a `coerce` parameter (e.g. the `$`/`@` operators) is removed.
- **Enum comparisons** — `int(Role) < 4` is rendered as `Role < ROLE_Authority`.
- **Constant-arithmetic folding** — arithmetic over constant operands is folded
  to a single literal: `1 + 1` → `2`, `(4 + 4) * 2` → `16`. Only `+`, `-`, `*`,
  and `/` are folded, following UnrealScript's own rules — the result is a float
  if either operand is a float, otherwise an integer wrapped to 32 bits with `/`
  truncating toward zero; division by zero is left unfolded. Partial folds work
  (`2 + x` where only part is constant). String (`$`/`@`), comparison and
  bitwise operators are deliberately excluded.
- **Negation inversion** — `!(A == B)` folds to `A != B` when the negated
  operand is a single, directly-invertible comparison (`==`, `!=`, `<`, `<=`,
  `>`, `>=`). Fuzzy compare (`~=`) and the logical connectives `&&`/`||` are
  excluded, so the `!` is only dropped when a real inverse operator exists.

#### Statement and control-flow simplifications

- **Loop reconstruction** — `goto` back-edges are lifted into `while` loops,
  and, where a loop variable is initialised before the loop and stepped as the
  last body statement, into `for` loops (with `goto`s turned into `continue`).
  A back-edge that targets a *step statement* just before the guard (the loop's
  init and step being the same statement, e.g. a binary-search `mid` recompute)
  is reconstructed into a `while` with the step emitted once before the loop and
  again at the end of the body.
- **Dead code after a transfer** — statements after an unconditional `return`,
  `break`, `continue`, or `goto` (up to the end of the block) are unreachable
  and are dropped. Obfuscators plant junk there — e.g. an unresolvable call — to
  trip up decompilers and break recompilation. Removal stops at a reachable jump
  target (a `J0x..:` label or a `case`/`default:`), which is preserved.
- **Constant-true asserts** — `assert(true)` and equivalents can never fire and
  are dropped as debug/anti-decompilation noise; a constant-false assert (which
  always fires) is kept.
- **Empty then/else folding** — an `if(C){}else{BODY}` head is rewritten to
  `if(!C){BODY}`, and empty `else {}` blocks are removed.
- **Brace elision** — a control body (`if`, `for`, `while`, `foreach`, `else`)
  holding exactly one *simple* (non-control) statement drops its braces, and
  `else { if … }` collapses to `else if`. Braces are only shed around a simple
  statement, so this never produces a dangling `else`; a nested `if` and an
  `else if` keep their braces.

#### Declaration simplifications

- **Unused constants** — `const` declarations whose name is never referenced
  are dropped.
- **Modifiers** — modifiers are stripped from local variables, and the
  `editconst` modifier is removed everywhere.
- **Dead replication** — a `replication` block that can never do anything is
  removed: a non-`Actor` class cannot replicate, and a block whose every
  condition is a constant `false` is dead.

> **Note on parenthesis flattening (always on):** independent of `-s`, the
> decompiler drops redundant parentheses around same-operator-group chains where
> the value is preserved — `((A || B) || C)` renders `A || B || C`, and, because
> `$` and `@` share a precedence group, `((A $ B) @ C)` renders `A $ B @ C`. A
> left operand is always safe (operators are left-associative); a right operand
> is unwrapped only when the operator reassociates, so non-associative shapes
> like `A - (B - C)` and cross-group `(A && B) || C` keep their parentheses.

> **Note:** Core's own intrinsic classes are defined natively and carry no
> script, so decompiling `Core.u` produces no `.uc` files. Point the tool at
> mod/content packages.

---

## `deobfuscate`

Recover readable identifiers for an obfuscated package by applying a name map
to its **name table**. Each entry is renamed in place, so every reference —
code tokens, `defaultproperties`, name constants — follows automatically,
because references store a name *index* rather than a copy of the string. The
result is a new `.u`; decompile it to obtain readable, round-trippable source.

The map file has one `<ObfuscatedToken> = <ResolvedName>` per line, with an
optional trailing `# ...` provenance comment; blank lines and `#` lines are
ignored. The obfuscated token on the left is used **verbatim** — its format is
not validated, so any symbol can be renamed — while each resolved name must be a
bare UnrealScript identifier. A token mapped to itself (still unresolved) is
skipped. If a header line carries a `# token-encoding: base64` marker (as
written by [`obfuscate -m`](#obfuscate) in harder mode), the token column is
base64-decoded back to its raw bytes before matching, so non-printable
obfuscated symbols round-trip losslessly.

Multiple obfuscated tokens may resolve to the same human name (the same source
name recurs across unrelated classes). Within a single function, though, a
parameter or local may not share a name with a member of its class/superclass
(or with a parameter of the same function), because the local would otherwise
*shadow* the member once recompiled and silently change behaviour. Such clashes
are resolved automatically by suffixing the offending parameter/local (the
member keeps its name); each de-collision is printed. The one unresolvable
case — two distinct *fields of the same class* mapped to one name — aborts with
an error.

```bash
python -m ut2004packageutil deobfuscate -i <UT2004.ini> -p <package.u> -m <map.txt> -o <out.u>
```

| Option            | Description                                          |
|-------------------|------------------------------------------------------|
| `-p`, `--package` | Obfuscated package to rename (required).             |
| `-m`, `--map`     | Path to the deobfuscation name map (required).       |
| `-o`, `--output`  | Output `.u` file (required).                         |

---

## `extract-source`

Emit one `<ClassName>.uc` file per class, but take the class body **verbatim
from the original source** the compiler embedded in the package (its
`ScriptText`) instead of lifting it from bytecode. The `defaultproperties`
block is reconstructed from the binary defaults exactly as `decompile` does,
because it is not part of the stored source text.

```bash
python -m ut2004packageutil extract-source -i <UT2004.ini> -p <package.u> -o <out_dir>
```

| Option              | Description                                          |
|---------------------|------------------------------------------------------|
| `-p`, `--package`   | Package to extract (required).                       |
| `-o`, `--output`    | Output **directory** for the `.uc` files (required). |

Use `extract-source` when the package still carries its source and you want the
author's exact formatting and comments; use [`decompile`](#decompile) when the
source was stripped or you want source reconstructed from bytecode. There is no
`-s`/`--simplify` option — the extracted body is the original text and is never
rewritten.

> **Note:** Classes whose source was stripped (e.g. by `obfuscate`, which
> replaces `ScriptText` with a placeholder) carry no usable source and are
> skipped; for those, use `decompile` instead.

---

## Exit behaviour

On error the tool prints `ERROR: <message>` and stops. The writing commands
(`obfuscate`, `decompile`, `deobfuscate`, …) print progress lines describing
what was written.
