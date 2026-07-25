# UT2004PackageUtil

A Python toolkit for reading, writing, and manipulating **Unreal Engine 2
(UT2004) `.u` packages**. It parses the full package structure — name,
import, and export tables, objects, tagged properties, and the UnrealScript
bytecode token stream — and can round-trip a package byte-for-byte.

On top of that it provides higher-level tools:

- **Info** — dump a package's name/import/export tables and object details.
- **XML export / import** — convert a package to an editable directory of XML
  (plus sidecar source/token files) and back to a `.u`. Embedded property-only
  components (e.g. GUI widgets) are inlined as editable properties rather than
  opaque binary blobs.
- **Obfuscate** — rename a package's code symbols to hinder reverse
  engineering, while preserving everything that must stay stable (imports,
  engine references, config/localized names, names reached by string,
  string-cast enums, the property names a stored texture/sound/shader/emitter/
  component addresses in its tagged data, etc.), plus `Commandlet` subclasses
  (which `ucc` resolves by name). Exports stop being public and their declarations are marked
  `private`/`editconst` (except anything you keep public via `-k`, or that a
  co-obfuscated sibling imports — the linker refuses to import a non-public
  object), `-r/--retain-privacy` marks them private outright, and an
  optional `-m/--map` writes an `obfuscated → original` name map that
  `deobfuscate` can consume to reverse it. `-a/--always` force-obfuscates
  named symbols. Repeating `-p`/`-o` obfuscates several packages as one
  family: shared symbols get the same token, so importing from a sibling no
  longer forces a readable name.
- **Deobfuscate** — apply a name map to an obfuscated package's name table to
  recover readable identifiers, auto-disambiguating parameter/local names that
  would otherwise shadow a class member once recompiled.
- **Decompile** — turn a package's classes back into `.uc` UnrealScript source
  (one file per class), with an optional `--simplify` pass that removes
  compiler-inserted noise (redundant casts, dead code, unused declarations, and
  every modifier on a local variable — which is what makes a decompile of an
  obfuscated package recompilable),
  folds constant expressions, and reconstructs `for`/`while`/`do..until` loops.
  An optional `-f/--display-offset` annotates each statement with its bytecode
  offset.

## Requirements

- Python **3.12+**
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`
- A UT2004 installation (or its `System/` directory) to resolve dependency
  packages via `UT2004.ini`.

## Installation

Using `uv` (recommended):

```bash
make install          # uv sync --extra dev
```

or directly:

```bash
uv sync --extra dev   # dev extras: pytest, ruff, mypy, pre-commit
```

With plain `pip`:

```bash
pip install -e .
```

## Quick start

The tool is a CLI invoked as a module:

```bash
python -m ut2004packageutil <command> [options]
# or, inside the uv environment:
uv run python -m ut2004packageutil <command> [options]
```

Every command needs `-i/--ini` pointing at your `UT2004.ini` so dependency
packages can be located. Some examples:

```bash
# Print everything about a package
python -m ut2004packageutil info \
    -i /path/to/UT2004/System/UT2004.ini \
    -p /path/to/UT2004/System/MyMod.u

# Decompile a package's classes to .uc source
python -m ut2004packageutil decompile \
    -i /path/to/UT2004/System/UT2004.ini \
    -p /path/to/UT2004/System/MyMod.u \
    -o out/MyMod

# Decompile with the clean-up pass enabled
python -m ut2004packageutil decompile -s \
    -i /path/to/UT2004/System/UT2004.ini \
    -p /path/to/UT2004/System/MyMod.u \
    -o out/MyMod

# Extract the original embedded source (kept verbatim) to .uc
python -m ut2004packageutil extract-source \
    -i /path/to/UT2004/System/UT2004.ini \
    -p /path/to/UT2004/System/MyMod.u \
    -o out/MyMod

# Obfuscate a package
python -m ut2004packageutil obfuscate \
    -i /path/to/UT2004/System/UT2004.ini \
    -p /path/to/UT2004/System/MyMod.u \
    -o /path/to/UT2004/System/MyMod_obf.u

# Obfuscate two packages that reference each other, as one family
python -m ut2004packageutil obfuscate \
    -i /path/to/UT2004/System/UT2004.ini \
    -p build/MyMod.u    -o /path/to/UT2004/System/MyMod.u \
    -p build/MyModPlus.u -o /path/to/UT2004/System/MyModPlus.u \
    -m /path/to/UT2004/System/MyMod.txt
```

See **[docs/USAGE.md](docs/USAGE.md)** for the full command reference and
option details.

## Commands at a glance

| Command      | Purpose                                                        |
|--------------|----------------------------------------------------------------|
| `info`       | Print name/import/export tables and object dumps.              |
| `xml-export` | Export a package to a directory of XML + sidecar files; `-f` adds bytecode offsets to the token disassembly. |
| `xml-import` | Rebuild a `.u` from an XML directory.                          |
| `obfuscate`  | Rename code symbols (simple/harder); repeat `-p`/`-o` to obfuscate a package family together, `-a` forces, `-k` keeps a field public, `-r` enforces private, `-m` emits a reverse map. |
| `decompile`  | Emit one `.uc` file per class; `-s` cleans up the output, `-f` shows bytecode offsets. |
| `deobfuscate`| Apply a name map to the name table (with de-collision).        |
| `extract-source` | Emit `.uc` from embedded class source + rebuilt defaults.   |

## Development

```bash
make format      # ruff format + import sort
make check       # ruff import check + mypy
make hooks       # install pre-commit hooks
make pre-commit  # run all pre-commit hooks
```

## Credits

This project builds on the excellent prior work of the Unreal community:

- **[UTPT](http://www.acordero.org/projects/unreal-tournament-package-tool/)**
  by **Antonio Cordero Balcázar** — reference documentation for the UT
  package file format, which informed this project's parser/writer.
- **[Unreal-Library (UELib)](https://github.com/EliotVU/Unreal-Library)**
  by **EliotVU** — reference implementation for the UnrealScript bytecode
  decompiler, which this project's decompiler is based on.

UnrealScript, Unreal Engine, and Unreal Tournament 2004 are trademarks of
Epic Games. This is an unofficial, fan-made tool and is not affiliated with or
endorsed by Epic Games. It does not include any copyrighted game content.

## License

Released under the [MIT License](LICENSE).
