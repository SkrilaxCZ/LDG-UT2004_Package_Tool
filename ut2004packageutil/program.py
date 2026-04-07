"""Main CLI entry point for UnPackageUtil.

Usage:
    python -m ut2004packageutil <command> [options]
"""

import argparse
import os
import sys
from typing import List, Optional

from ut2004packageutil.obfuscator import ObfuscationType, Obfuscator
from ut2004packageutil.package.flags import (
    object_flags_to_string,
    package_flags_to_string,
)
from ut2004packageutil.package.io import UnPackageIO
from ut2004packageutil.package.package import UnPackage
from ut2004packageutil.package.package_loader import PackageLoader


def get_string(text: str, min_length: int) -> str:
    """Right-pad a string with spaces to a minimum length.

    Args:
        text (str): The text to pad.
        min_length (int): The minimum length of the resulting string.

    Returns:
        str: The text padded with trailing spaces to at least min_length.
    """
    pad = max(min_length - len(text), 0)
    return text + " " * pad


# ===================================================================== #
#  Command handlers
# ===================================================================== #


def _get_loader(args: argparse.Namespace) -> PackageLoader:
    """Create a PackageLoader from CLI arguments.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        PackageLoader: A loader configured from the INI and base directory.
    """
    return PackageLoader(args.ini, base_dir=getattr(args, "base_directory", None))


def _load_package(args: argparse.Namespace) -> "UnPackage":
    """Load a package with dependencies using CLI arguments.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        "UnPackage": The loaded package with its dependencies resolved.
    """
    loader = _get_loader(args)
    return loader.load_with_dependencies(args.package)


def cmd_info(args: argparse.Namespace) -> None:
    """Print detailed information about a package.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    pkg = _load_package(args)

    delimiter = "=" * 360
    delimiter2 = "-" * 360

    print(f"Package:     {args.package}")
    print(f"Flags:       {package_flags_to_string(pkg.flags)}")
    print(f"GUID:        {pkg.guid}")

    print(delimiter)
    print("NAMES")
    print(delimiter)

    name_str = "Name"
    name_len = len(name_str)
    flag_str = "Flags"
    flag_len = len(flag_str)
    index_str = "Index"
    index_len = len(index_str)

    for i, n in enumerate(pkg.names):
        name_len = max(name_len, len(n.name))
        flag_len = max(flag_len, len(object_flags_to_string(n.flags)))
        index_len = max(index_len, len(str(i)))

    print(
        f"| {get_string(index_str, index_len)}\t"
        f"| {get_string(name_str, name_len)}\t"
        f"| {get_string(flag_str, flag_len)}\t|"
    )
    print(delimiter2)

    for i, n in enumerate(pkg.names):
        print(
            f"| {get_string(str(i), index_len)}\t"
            f"| {get_string(n.name, name_len)}\t"
            f"| {get_string(object_flags_to_string(n.flags), flag_len)}\t|"
        )

    print(delimiter)
    print("IMPORTS")
    print(delimiter)

    name_len = len("Name")
    class_str = "Class"
    class_len = len(class_str)
    group_str = "Group"
    group_len = len(group_str)

    for imp in pkg.imports:
        name_len = max(name_len, len(imp.object_name.name))
        pkg_name = imp.class_package_name.name if imp.class_package_name else ""
        cls_name = imp.class_name.name if imp.class_name else ""
        class_len = max(class_len, len(pkg_name) + len(cls_name) + 1)
        if imp.group_item is not None:
            group_len = max(group_len, len(imp.group_item.object_name_string))

    print(
        f"| {get_string('Name', name_len)}\t"
        f"| {get_string(class_str, class_len)}\t"
        f"| {get_string(group_str, group_len)}\t|"
    )
    print(delimiter2)

    for imp in pkg.imports:
        pkg_name = imp.class_package_name.name if imp.class_package_name else ""
        cls_name = imp.class_name.name if imp.class_name else ""
        group = imp.group_item.object_name_string if imp.group_item else ""
        print(
            f"| {get_string(imp.object_name.name, name_len)}\t"
            f"| {get_string(pkg_name + '.' + cls_name, class_len)}\t"
            f"| {get_string(group, group_len)}\t|"
        )

    print(delimiter)
    print("EXPORTS")
    print(delimiter)

    name_len = len("Name")
    flag_len = len("Flags")
    class_len = len("Class")
    object_name_str = "Object Name"
    object_name_len = len(object_name_str)
    super_str = "Super"
    super_len = len(super_str)
    group_len = len("Group")

    for exp in pkg.exports:
        name_len = max(name_len, len(exp.object_name.name))
        flag_len = max(flag_len, len(object_flags_to_string(exp.flags)))
        object_name_len = max(object_name_len, len(exp.object_name_string))
        class_len = max(class_len, len(exp.class_name_string))
        if exp.super_item is not None:
            super_len = max(super_len, len(exp.super_item.object_name_string))
        if exp.group_item is not None:
            group_len = max(group_len, len(exp.group_item.object_name_string))

    print(
        f"| {get_string('Name', name_len)}\t"
        f"| {get_string(object_name_str, object_name_len)}\t"
        f"| {get_string('Class', class_len)}\t"
        f"| {get_string('Flags', flag_len)}\t"
        f"| {get_string(super_str, super_len)}\t"
        f"| {get_string('Group', group_len)}\t"
        f"| Data Size \t|"
    )
    print(delimiter2)

    for exp in pkg.exports:
        super_name = exp.super_item.object_name_string if exp.super_item else ""
        group_name = exp.group_item.object_name_string if exp.group_item else ""
        data_size = len(exp.export_data) if exp.export_data else 0
        print(
            f"| {get_string(exp.object_name.name, name_len)}\t"
            f"| {get_string(exp.object_name_string, object_name_len)}\t"
            f"| {get_string(exp.class_name_string, class_len)}\t"
            f"| {get_string(object_flags_to_string(exp.flags), flag_len)}\t"
            f"| {get_string(super_name, super_len)}\t"
            f"| {get_string(group_name, group_len)}\t"
            f"| 0x{data_size:08X}\t|"
        )

    print(delimiter)
    print("OBJECTS")
    print(delimiter)

    for exp in pkg.exports:
        if exp.object is not None:
            print(exp.object.dump())

    print(delimiter)


def cmd_xmlexport(args: argparse.Namespace) -> None:
    """Export package data as a directory with Package.xml and sidecar files.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    pkg = _load_package(args)
    rdr = UnPackageIO()
    rdr.export_xml(pkg, args.output, drop_generations=args.no_generations)


def cmd_xmlimport(args: argparse.Namespace) -> None:
    """Import package data from a directory containing Package.xml.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    _get_loader(args)  # registers global singleton
    wrt = UnPackageIO()
    pkg = wrt.import_xml(args.input)
    wrt.write_package(pkg, args.output)


def cmd_obfuscate(args: argparse.Namespace) -> None:
    """Obfuscate package names.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    pkg = _load_package(args)

    obf_type = ObfuscationType.HARDER
    if args.simple:
        obf_type |= ObfuscationType.SIMPLE

    exp_list: List[str] = []
    if args.exceptions and os.path.isfile(args.exceptions):
        with open(args.exceptions, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    exp_list.append(line)

    print(f"Obfuscating {args.package} ...")

    obf = Obfuscator()
    obf.obfuscate(pkg, obf_type, exp_list)
    obf.strip_source(pkg)

    wrt = UnPackageIO()
    wrt.write_package(pkg, args.output)

    print(f"Written obfuscated package to {args.output}.")

    if args.map:
        count = obf.write_name_map(args.map)
        print(f"Wrote {count} name mapping(s) to {args.map}.")


def cmd_decompile(args: argparse.Namespace) -> None:
    """Decompile a package's classes into .uc source files.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    from ut2004packageutil.decompiler import Decompiler

    pkg = _load_package(args)

    print(f"Decompiling {args.package} ...")
    decompiler = Decompiler(pkg, simplify=args.simplify)
    written = decompiler.decompile_to_folder(args.output)
    for path in written:
        print(f"  wrote {path}")
    print(f"Decompiled {len(written)} class(es) to {args.output}.")


def cmd_deobfuscate(args: argparse.Namespace) -> None:
    """Apply a name map to a package's name table and save the result.

    Renames the symbols in place (references follow by name index) and
    disambiguates any parameter/local that would shadow a class/superclass
    member once recompiled, then writes a new ``.u``.  Decompile the output to
    obtain readable, round-trippable source.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    from ut2004packageutil.deobfuscator import apply_name_map, parse_map_file

    pkg = _load_package(args)
    mapping = parse_map_file(args.map)
    print(f"Deobfuscating {args.package} with {len(mapping)} mapped name(s) ...")
    report = apply_name_map(pkg, mapping)
    for fn, token, want, final in report.renames:
        print(f"  de-collide {fn}: {token} -> {final} (wanted {want})")
    print(
        f"Renamed {report.applied} name-table entries "
        f"({len(report.renames)} param/local de-collision(s))."
    )

    wrt = UnPackageIO()
    wrt.write_package(pkg, args.output)
    print(f"Written deobfuscated package to {args.output}.")


def cmd_extractsource(args: argparse.Namespace) -> None:
    """Extract embedded class source into .uc files with reconstructed defaults.

    Args:
        args (argparse.Namespace): Parsed command-line arguments.
    """
    from ut2004packageutil.decompiler import Decompiler

    pkg = _load_package(args)

    print(f"Extracting source from {args.package} ...")
    decompiler = Decompiler(pkg)
    written = decompiler.extract_source_to_folder(args.output)
    for path in written:
        print(f"  wrote {path}")
    print(f"Extracted source for {len(written)} class(es) to {args.output}.")


# ===================================================================== #
#  Argument parser setup
# ===================================================================== #


def _add_common_args(sub: argparse.ArgumentParser) -> None:
    """Add the common -i/--ini and -b/--base-directory arguments to a subparser.

    Args:
        sub (argparse.ArgumentParser): The subparser to add arguments to.
    """
    sub.add_argument(
        "-i", "--ini", required=True, help="Path to the UT2004.ini configuration file"
    )
    sub.add_argument(
        "-b",
        "--base-directory",
        default=None,
        help="Optional base directory for package search (searched before INI dir)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all command subparsers.

    Returns:
        argparse.ArgumentParser: The configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="ut2004packageutil",
        description="UT2004 Package Utility — read, write, and manipulate Unreal .u packages.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # info
    p_info = subparsers.add_parser("info", help="Print information about a package")
    _add_common_args(p_info)
    p_info.add_argument(
        "-p", "--package", required=True, help="Path to the .u package file"
    )

    # xmlexport
    p_xmlexport = subparsers.add_parser(
        "xml-export", help="Export data as a directory with Package.xml"
    )
    _add_common_args(p_xmlexport)
    p_xmlexport.add_argument(
        "-p", "--package", required=True, help="Path to the .u package file"
    )
    p_xmlexport.add_argument(
        "-o", "--output", required=True, help="Output directory path"
    )
    p_xmlexport.add_argument(
        "-g",
        "--no-generations",
        action="store_true",
        help="Drop generation history and GUID (will be regenerated on import)",
    )

    # xmlimport
    p_xmlimport = subparsers.add_parser(
        "xml-import", help="Import data from a directory with Package.xml"
    )
    _add_common_args(p_xmlimport)
    p_xmlimport.add_argument(
        "-d", "--input", required=True, help="Input directory path"
    )
    p_xmlimport.add_argument(
        "-o", "--output", required=True, help="Output .u package file"
    )

    # obfuscate
    p_obfuscate = subparsers.add_parser("obfuscate", help="Obfuscate package names")
    _add_common_args(p_obfuscate)
    p_obfuscate.add_argument(
        "-p", "--package", required=True, help="Path to the .u package file"
    )
    p_obfuscate.add_argument(
        "-s",
        "--simple",
        action="store_true",
        help="Use simple obfuscation (default: harder)",
    )
    p_obfuscate.add_argument(
        "-e", "--exceptions", help="Path to exceptions file (one name per line)"
    )
    p_obfuscate.add_argument("-o", "--output", required=True, help="Output .u file")
    p_obfuscate.add_argument(
        "-m",
        "--map",
        default=None,
        help="Optional path to write an obfuscated->original name map "
        "(feedable to the deobfuscate command); omit to skip writing it",
    )

    # decompile
    p_decompile = subparsers.add_parser(
        "decompile", help="Decompile package classes into .uc source files"
    )
    _add_common_args(p_decompile)
    p_decompile.add_argument(
        "-p", "--package", required=True, help="Path to the .u package file"
    )
    p_decompile.add_argument(
        "-o", "--output", required=True, help="Output folder for .uc files"
    )
    p_decompile.add_argument(
        "-s",
        "--simplify",
        action="store_true",
        help="Clean up anti-decompilation noise (unused consts, redundant "
        "casts, no-op asserts, local/editconst modifiers, reconstruct while "
        "loops)",
    )

    # deobfuscate
    p_deobfuscate = subparsers.add_parser(
        "deobfuscate",
        help="Apply a name map to a package's name table (with de-collision)",
    )
    _add_common_args(p_deobfuscate)
    p_deobfuscate.add_argument(
        "-p", "--package", required=True, help="Path to the .u package file"
    )
    p_deobfuscate.add_argument(
        "-m", "--map", required=True, help="Path to the deobfuscation name map"
    )
    p_deobfuscate.add_argument("-o", "--output", required=True, help="Output .u file")

    # extractsource
    p_extractsource = subparsers.add_parser(
        "extract-source",
        help="Extract embedded class source into .uc files (with defaults)",
    )
    _add_common_args(p_extractsource)
    p_extractsource.add_argument(
        "-p", "--package", required=True, help="Path to the .u package file"
    )
    p_extractsource.add_argument(
        "-o", "--output", required=True, help="Output folder for .uc files"
    )

    return parser


COMMAND_MAP = {
    "info": cmd_info,
    "xml-export": cmd_xmlexport,
    "xml-import": cmd_xmlimport,
    "obfuscate": cmd_obfuscate,
    "decompile": cmd_decompile,
    "deobfuscate": cmd_deobfuscate,
    "extract-source": cmd_extractsource,
}


def main(args: Optional[List[str]] = None) -> None:
    """Parse command-line arguments and dispatch to the matching handler.

    Args:
        args (Optional[List[str]]): Argument list to parse. Defaults to
            sys.argv[1:] when None.
    """
    if args is None:
        args = sys.argv[1:]

    parser = build_parser()
    parsed = parser.parse_args(args)

    if parsed.command is None:
        parser.print_help()
        return

    handler = COMMAND_MAP.get(parsed.command)
    if handler is None:
        parser.print_help()
        return

    try:
        handler(parsed)
    except Exception as ex:
        print(f"ERROR: {ex}")


if __name__ == "__main__":
    main()
