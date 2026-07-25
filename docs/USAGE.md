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

Embedded objects whose class the tool does not model but whose payload is a
plain tagged-property list (e.g. GUI components such as `GUIButton`/`GUILabel`)
are written **inline** as structured `<ObjectData>` — just like
`defaultproperties` — instead of an opaque `Raw<Class>/<name>.bin` sidecar.
Only pure property containers that round-trip byte-for-byte are inlined;
anything with trailing native data (textures, meshes, sounds, …) still lands
in a `Raw<Class>/` binary sidecar.

```bash
python -m ut2004packageutil xml-export -i <UT2004.ini> -p <package.u> -o <out_dir> [-g] [-f]
```

| Option                | Description                                                             |
|-----------------------|-------------------------------------------------------------------------|
| `-p`, `--package`     | Package to export (required).                                           |
| `-o`, `--output`      | Output **directory** (required).                                        |
| `-g`, `--no-generations` | Drop generation history and the GUID (both regenerated on import).   |
| `-f`, `--display-offset` | Prefix each line of the `.uasm` token disassembly with a `/* %04X */` byte-offset comment. The comment is purely informational and is ignored by `xml-import`, so an exported/re-imported package is still byte-identical. |

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
state names, native symbols and native-class member variables, names observed
as a string in the bytecode, and enums any of whose values are cast to a
string (the whole enum — type name and every value — is kept, since the cast
yields the member name at runtime).

Also preserved: every property name a **stored object's data** addresses. A
texture, sound, shader, final blend, emitter or embedded GUI component keeps its
values in a *tagged* stream — each value prefixed by the name of the engine
property it sets — and the engine resolves those names by string when the object
loads, so renaming one silently corrupts the object (a local `Format` parameter
used to rename a texture's `Engine.Bitmap.Format` tag, wrecking its pixel
format). Names nested inside a struct or array-of-struct value count too
(`RangeVector` → `X` → `Range` → `Min`), as does a local class's
`defaultproperties` tag naming an *inherited* engine property. A tag naming a
property this package declares itself is still renamed: definition and tag share
one name entry, so they move together. When a local symbol merely *shares* the
name, it is split onto an entry of its own and obfuscated as usual — the rule
costs no coverage.

In a family run (see [Obfuscating a package
family](#obfuscating-a-package-family)) the same holds **across** the members: a
subclass storing a default for a property its superclass declares one package
over addresses it by name, with no import to speak for the link, so the property
keeps its name in the package that declares it as well. Those names are listed at
the start of the run (`Preserving for a sibling's stored data: …`). Getting this
wrong is invisible at load time — the engine finds no property of that name,
skips the value and leaves the inherited default in place — which is why both
ends are preserved rather than renamed together.

Every export stops being public (the `Public` flag is cleared) so the
obfuscated package doesn't re-export its renamed symbols — in both `--simple`
and the default harder mode. Three things are exempt: a symbol named via
`-k/--keep-public`, whose `Public` flag is retained so an outside package can
still resolve it; anything another package of the same run imports (see
[Obfuscating a package family](#obfuscating-a-package-family)); and any property
this package itself reaches through `SetPropertyText`/`GetPropertyText`. That
last one is not optional: the engine's lookup requires the flag as well as the
name (and the property not to be `const`), and without it the call silently
does nothing — the property reads back as `None`.
Those properties keep their name, their `Public` flag, and their declaration.
`-r/--retain-privacy` goes further and marks each hidden export **`Private`**.

The **declarations** are hardened to match, since dropping `Public` only hides an
export from the linker and leaves it reading as public API. Using the same
encodings `ucc` does, every non-exempt declaration becomes:

- a variable — member, struct field or function *local* alike — `private`
  (`RF_Final`, no `RF_Public`) and `editconst` (`CPF_EditConst`);
- a function `private` (`FUNC_Private`, no `FUNC_Public`).

Parameters and return values are left alone: a modifier is not legal on them, and
an `out` parameter has to stay writable. None of this changes runtime behaviour —
those flags are read by the compiler and the editor, never by the VM (property
access is by offset, function dispatch by index) — but it does mean a decompile of
the obfuscated package no longer recompiles: `ucc` disallows *every* type modifier
on a local (`Disallow = ~0` → *"Specified type modifiers not allowed here"*), so a
recovered `local private editconst int i;` has to be cleaned up by hand first.

```bash
python -m ut2004packageutil obfuscate -i <UT2004.ini> -p <package.u> -o <out.u> [-s] [-e <file>] [-a <file>] [-k <file>] [-r] [-m <map.txt>]

# a whole family in one pass (one -o per -p, paired in order)
python -m ut2004packageutil obfuscate -i <UT2004.ini> \
    -p build/MyMod.u     -o System/MyMod.u \
    -p build/MyModPlus.u -o System/MyModPlus.u -s -m System/MyMod.txt
```

| Option              | Description                                                                       |
|---------------------|-----------------------------------------------------------------------------------|
| `-p`, `--package`   | Package to obfuscate (required). Repeatable — see [Obfuscating a package family](#obfuscating-a-package-family). |
| `-o`, `--output`    | Output `.u` file (required). One per `-p`, paired in order; a count mismatch is an error. |
| `-s`, `--simple`    | Use *simple* obfuscation (INI-safe glyph symbols; see below). Default is the *harder* mode. |
| `-e`, `--exceptions`| Path to a text file of names to preserve (one per line, matched case-insensitively). |
| `-a`, `--always`    | Path to a text file of symbols to **always** obfuscate (one per line), overriding every preservation rule. Forced symbols use the simple glyph form even in harder mode, so they can never corrupt an INI/config key. |
| `-k`, `--keep-public` | Path to a text file of symbols (one per line; bare `Name` or qualified `Class.Field`, matched case-insensitively) whose `Public` flag is **kept**, so runtime reflection (`SetPropertyText`/`GetPropertyText`) — or a package outside this run — can still resolve them. If such a symbol is itself obfuscated, it uses the simple glyph form so the exported name stays clean. |
| `-r`, `--retain-privacy` | Mark every hidden export `Private` rather than merely dropping its `Public` flag. Errors out if a sibling in the same run imports a **property** (see below). |
| `-m`, `--map`       | Optional path to write an `obfuscated → original` name map. Omit to skip it. One map covers a whole family. |

The command prints, per original name, whether it was `Excluding` (preserved,
with the reason) or `Hashing` (rewritten, with the resulting symbol(s)).

### Obfuscating a package family

Repeat `-p`/`-o` to obfuscate several packages that reference each other as one
family. Normally an import pins the imported symbol's name — the linker matches
it by string — so a package that another package builds on could barely be
obfuscated at all. In a shared run both ends are renamed together instead:

- The family is processed **dependency-first**, derived from the import tables
  (the order is printed); an import cycle is an error.
- Every symbol a sibling can reach by string — anything it imports, plus
  anything named in bytecode or property data — is renamed to the **same
  token** in every package that mentions it. One symbol allocator drives the
  whole run, so the tokens stay unique, and one `-m` map covers everything.
- Consequently the import/external-superclass/function-override exclusions
  **stop applying** to references into a sibling. Symbols reached only through
  an export index (function locals and parameters above all) are still renamed
  per package, so the shared set stays small.
- Each package is matched to its siblings' import entries by its **file
  basename**. Pass the files named exactly as the imports spell them — if your
  build keeps the readable package as `MyMod-dev.u`, stage a copy named
  `MyMod.u` first, or the sibling's import resolves through the INI search
  paths to a *different* build of that package.
- If a sibling renames a symbol whose name this package has to keep for an
  unrelated reason, the run stops with an error naming the symbol; list it in
  `-e/--exceptions` so both packages keep it readable.

Export visibility follows the family too. **Anything a sibling imports keeps its
`Public` flag**, whatever its kind — UE2's linker verifies every import against
the exporting package and refuses one whose target is not public:

```
Failed to load MyLoader: Can't import private object
    Function MyMod.MyBaseClass.Ol2IlS0OOS1S512O
    (when loading Class'MyModPlus.MyDerivedClass')
```

That is a check on the *object flag* and it fires for classes, properties and
functions alike; it is not the same thing as the script `private` modifier,
which only gates property access. Under `-r/--retain-privacy` a sibling-imported
**property** is therefore reported as an error (you have to decide: reach it
through a function, list it in `-k/--keep-public`, or drop `-r`), while the other
kinds simply stay public because they cannot be hidden at all.

A package that imports from the family but is **not** in the run gets no such
treatment — the obfuscator cannot see it. Keep its entry points reachable by
listing them in `-e/--exceptions` (stable name) and `-k/--keep-public` (public
flag), and verify by actually loading it.

### Simple vs harder mode

- **Simple** (`-s`) rewrites each symbol to a 16-character, `O`-bookended glyph
  token drawn from the set `O 0 1 l I 7 2 5 S T` (e.g. `O0lI7…S25O`). The 14
  interior characters are drawn at random — the run remembers what it issued and
  redraws a repeat, so nothing about a token says when it was allocated. Every
  character is printable and identifier-legal, so a rewritten name is safe to use
  verbatim as an INI/config key; the leading character is a letter because five
  of the ten glyphs are digits.
- **Harder** (default) rewrites each symbol to a non-printable hash containing
  newlines and control bytes — harder to read or transcribe, but not safe as an
  INI key (hence `-a`/`-k` symbols fall back to the glyph form).

### The `-m/--map` reverse map

When `-m` is given, one `<ObfuscatedToken> = <OriginalName>` line is written per
rewritten name. Feed the file straight to [`deobfuscate`](#deobfuscate) to rename
the package back to its original symbols. Simple-mode glyph tokens are written
verbatim; the default harder mode produces non-printable hashes (newlines and
control bytes) that can't sit on a text line, so the token column is
**base64-encoded** and the header carries a `# token-encoding: base64` marker
that `deobfuscate` recognises automatically.

A shared run writes a single map for the whole family: the tokens are shared, so
a symbol present in several packages needs only one line, and the same map file
deobfuscates each package of the family.

---

## `decompile`

Decompile a package's classes back into `.uc` UnrealScript source, writing one
`<ClassName>.uc` file per class into the output folder. Class declarations,
constants, enums, structs, variables, replication blocks, functions (with
decompiled bodies), states, and `defaultproperties` are all reconstructed.

```bash
python -m ut2004packageutil decompile -i <UT2004.ini> -p <package.u> -o <out_dir> [-s] [-f]
```

| Option              | Description                                          |
|---------------------|------------------------------------------------------|
| `-p`, `--package`   | Package to decompile (required).                     |
| `-o`, `--output`    | Output **directory** for the `.uc` files (required). |
| `-s`, `--simplify`  | Enable the clean-up pass (see below).                |
| `-f`, `--display-offset` | Prefix each statement with its bytecode offset (see below). Cannot be combined with `-s`. |

### The `-f/--display-offset` listing

Prefixes each statement line with a `/* %04X */` comment giving the byte offset
(within the function/state's bytecode) of the token that begins it; offsets
restart at `0000` for each function. To keep the code aligned, other lines
inside a function/state block (declaration header, braces, locals, labels) are
left-padded with a matching blank gutter, while class-level lines (the class
header, member variables, `defaultproperties`) are not. This produces a
disassembly-style listing that is **not** recompilable — it is for inspection.

It **cannot** be combined with `-s`: the `--simplify` pass reorders and folds
statements, so a per-statement offset would no longer line up with the
bytecode. Requesting both is rejected with an error.

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
- **Modifiers** — **every** modifier is stripped from a local variable
  declaration — its type modifiers *and* its `private`/`protected` access
  specifier — the `editconst` modifier is removed everywhere, and `private` is
  dropped from **struct members**.
  This is what keeps a decompile of an *obfuscated* package recompilable:
  obfuscation marks locals `private editconst`, and `ucc` allows no type modifier
  at all on a local (`Disallow = ~0` → *"Specified type modifiers not allowed
  here"*). The struct-member case is subtler — `ucc` parses `var private int A;`
  inside a `struct` happily, it just clears `RF_Public` on the field, and the
  build then dies at save time the moment another package references that member
  ("Referencers of IntProperty `Pkg.Class.Struct.Member`:", non-zero exit, no line
  number to go on). A *class* variable's `private` is honoured and kept, as is
  `protected` anywhere (it leaves `RF_Public` set). Without `-s` every modifier is
  rendered faithfully, and the recovered source has to be cleaned up by hand
  before it will compile.
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
