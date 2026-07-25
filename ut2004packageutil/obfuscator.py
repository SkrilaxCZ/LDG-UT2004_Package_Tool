"""Package obfuscation utilities."""

import io
import os
import random
from enum import Enum, IntFlag
from typing import Any, Dict, List, Optional, Set, Tuple

from ut2004packageutil.deobfuscator import TOKEN_ENCODING_MARKER, encode_map_token
from ut2004packageutil.package.flags import (
    UnFunctionFlags,
    UnNameMap,
    UnObjectFlags,
    UnPropertyFlags,
    UnStructFlags,
)
from ut2004packageutil.package.object import (
    CODE_CLASS_NAMES,
    UnByteProperty,
    UnClass,
    UnDefaultObject,
    UnEnum,
    UnField,
    UnFunction,
    UnProperty,
    UnPropertyTag,
    UnStruct,
    UnTextBuffer,
    _find_array_inner_info,
    _is_native_serialize_struct,
)
from ut2004packageutil.package.package import (
    UnExport,
    UnImport,
    UnName,
    UnPackage,
    UnPackageItem,
    resolve_item,
)
from ut2004packageutil.package.token import (
    UnCastType,
    UnTokenDelegateFunction,
    UnTokenDelegateProperty,
    UnTokenFinalFunction,
    UnTokenGlobalFunction,
    UnTokenLabelTable,
    UnTokenNameConst,
    UnTokenPrimitiveCast,
    UnTokenStringConst,
    UnTokenVirtualFunction,
)
from ut2004packageutil.utils.io_utils import read_index, write_index


class ObfuscationType(IntFlag):
    """Flag controlling the level and style of obfuscation."""

    HARDER = 0x00
    SIMPLE = 0x01


class ObfuscationStatus(Enum):
    """Disposition of a single name during obfuscation.

    ``OBFUSCATED`` means the name was rewritten. Every other value
    explains why the name was preserved.
    """

    CONFIG_LOCALIZED_CLASS = "config/localized class"
    CONFIG_LOCALIZED_INI = "config/localized INI section"
    CONFIG_LOCALIZED_PROPERTY = "config/localized property"
    CONFIG_INHERITED_CLASS = "config-inheriting class"
    CORE_REFERENCE = "Core reference"
    COMMANDLET_CLASS = "commandlet class"
    EXCEPTION = "user exception"
    EXTERNAL_FUNCTION_NAME = "external function name"
    EXTERNAL_FUNCTION_OVERRIDE = "external function override"
    EXTERNAL_REFERENCE = "external reference"
    EXTERNAL_SUPERCLASS = "external superclass"
    FUNCTION_INVOKED_BY_NAME = "function invoked by name"
    IMPORT_REFERENCE = "import reference"
    NATIVE = "native"
    NATIVE_CLASS_VARIABLE = "native class variable"
    NONE_NAME = "'None' sentinel"
    OBFUSCATED = "obfuscated"
    OBJECT_DATA_TAG = "object data property tag"
    SIBLING_DATA_TAG = "sibling stored-data property tag"
    STATE_NAME = "state name"
    STRINGIFIED_ENUM = "stringified enum"
    STRINGIFIED_NAME = "stringified name"


class SharedObfuscationError(RuntimeError):
    """A package family cannot be obfuscated together as requested."""


class Obfuscator:
    """Rename exported names in an Unreal package to hinder reversing.

    The rewriter never relies on a property cache: every decision is
    made from the package's own export/import tables and from any
    dependency packages that were loaded together with the primary
    package via :class:`PackageLoader`.

    The disposition of every name in the name table is recorded in
    :attr:`name_status` (a mapping from :class:`UnName` to
    :class:`ObfuscationStatus`) so callers can introspect why a name was
    preserved.
    """

    def __init__(self) -> None:
        """Initialise the obfuscator with empty state and a fresh RNG."""
        self._hash_index: int = 0
        self._seeded: bool = False
        self._gen = random.Random()
        # Every glyph token issued in this run, so a repeat draw can be redrawn.
        # One obfuscator drives a whole family, so this covers it (see
        # :meth:`gen_simple`).
        self._issued_simple: Set[str] = set()
        self._reflection_protected: Set[int] = set()
        self.name_status: Dict[int, Tuple["UnName", "ObfuscationStatus"]] = {}
        # id(UnName) -> the name's string *before* the rewrite pass, captured
        # after names are un-shared.  Lets callers recover the original text
        # of each surviving entry (including freshly split copies).
        self.original_names: Dict[int, str] = {}
        # Simple names of the packages being obfuscated together with the
        # current one (see :meth:`obfuscate_packages`).  Empty for a plain
        # single-package run.
        self._co_obfuscated: Set[str] = set()
        # obfuscated token -> original name, accumulated across every
        # :meth:`obfuscate` call so one map file covers a whole family.
        self._map_entries: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    #  Hash generators
    # ------------------------------------------------------------------ #

    # 10 visually-confusable glyphs, ordered so their positions double as
    # base-10 digit values (O=0, 0=1, 1=2, l=3, I=4, 7=5, 2=6, 5=7, S=8, T=9).
    # Encoding the running index in this alphabet keeps every symbol unique
    # while the whole 16-char string reads as near-indistinguishable noise.
    # Used by simple mode: printable, whitespace-free and identifier-legal, so
    # it never corrupts an INI/config key.
    _SIMPLE_ALPHABET = "O01lI725ST"

    def gen_hash(self) -> str:
        """Generate a unique, hard-to-read symbol string (harder mode).

        Returns:
            str: A newline-wrapped symbol built from the current hash
                index and random digits, used as an obfuscated name.
        """
        hash_str = ["\x00"] * 14
        div = 1
        for i in range(8):
            digit = (self._hash_index // div) % 10
            hash_str[i] = chr(2 + digit)
            div *= 10
        for i in range(8, len(hash_str)):
            hash_str[i] = chr(2 + self._gen.randint(0, 9))
        hash_str[8 + self._gen.randint(0, len(hash_str) - 9)] = "\n"
        hash_str[8 + self._gen.randint(0, len(hash_str) - 9)] = "\n"
        self._hash_index += self._gen.randint(0, 999)
        return "\n" + "".join(hash_str) + "\n"

    def gen_simple(self) -> str:
        """Generate a unique, INI-safe glyph symbol (simple mode).

        The symbol is exactly 16 characters: a leading and trailing ``O`` with 14
        characters drawn at random from :attr:`_SIMPLE_ALPHABET`
        (``O 0 1 l I 7 2 5 S T``) in between. Every character is printable and
        identifier-legal, so the symbol is safe to use verbatim as an INI/config
        key — and the leading ``O`` is a letter on purpose: five of the ten
        glyphs are digits, and a field name starting with one is what an
        illegal-name scan counts.

        Uniqueness is by bookkeeping rather than by construction: every symbol
        the run has issued is remembered and a repeat draw is redrawn. The
        alternative — encoding a running counter in the first glyphs — leaves a
        recognisable pattern (a six-digit counter renders its two unused digits
        as ``OO``, the same two positions in every symbol) and, worse, is
        trivially decoded back into the order the symbols were allocated in,
        which tells a reader that adjacent tokens are related symbols.

        Returns:
            str: A 16-character ``O``-bookended symbol used as an obfuscated
                name.
        """
        alphabet = self._SIMPLE_ALPHABET
        while True:
            token = "O" + "".join(self._gen.choices(alphabet, k=14)) + "O"
            if token not in self._issued_simple:
                self._issued_simple.add(token)
                return token

    # ------------------------------------------------------------------ #
    #  Helpers — package & lookup utilities
    # ------------------------------------------------------------------ #

    @staticmethod
    def _simple_name(pkg: "UnPackage") -> str:
        """Return the package's simple basename (e.g. ``MyPackage``).

        Args:
            pkg (UnPackage): The package whose basename is requested.

        Returns:
            str: The basename with any directory and extension removed.
        """
        base = os.path.basename(pkg.name)
        return os.path.splitext(base)[0]

    @staticmethod
    def _find_class_export(pkg: "UnPackage", class_name: str) -> Optional["UnExport"]:
        """Look up a Class/Struct export by its short object name.

        Args:
            pkg (UnPackage): The package to search.
            class_name (str): The short object name to match.

        Returns:
            Optional[UnExport]: The matching export, or ``None`` if absent.
        """
        for exp in pkg.exports:
            if (
                exp.class_name_string in ("Core.Class", "Core.Struct")
                and exp.object_name.name == class_name
            ):
                return exp
        return None

    @staticmethod
    def _root_package_of_import(imp: "UnImport") -> str:
        """Return the name of the actual package an import lives in.

        Imports are organised hierarchically: the top of an import's
        ``group_item`` chain is a top-level ``Package``-class import
        whose ``object_name`` names the source ``.u`` file. When the
        import itself has no group it is already that top-level entry.
        ``imp.class_package_name`` is not the source package — it names
        the package that defines the import's class (always ``Core`` for
        the built-in classes ``Class``, ``Struct``, ``Function``,
        ``Package``, the property type classes, etc.).

        Args:
            imp (UnImport): The import whose root package is resolved.

        Returns:
            str: The source package name, or ``""`` if it cannot be found.
        """
        cur: Optional["UnPackageItem"] = imp
        while (
            cur is not None and isinstance(cur, UnImport) and cur.group_item is not None
        ):
            cur = cur.group_item
        if isinstance(cur, UnImport):
            return cur.object_name.name
        return ""

    def _is_co_obfuscated_import(self, imp: "UnImport") -> bool:
        """Whether ``imp`` refers into a package obfuscated in the same run.

        Such an import needs no name preservation: the symbol it names is
        being renamed in its own package too, and both packages draw the
        replacement from the same shared symbol map, so the link is kept by
        renaming *both* ends in lockstep.  The top-level ``Package`` entry
        itself is excluded — that one names the dependency's **file**, which
        is not ours to rename.

        Args:
            imp (UnImport): The import table entry to classify.

        Returns:
            bool: True when the import points into a co-obfuscated sibling
                and is not that sibling's top-level package entry.
        """
        if not self._co_obfuscated:
            return False
        if imp.group_item is None:
            # A top-level entry: either the package file itself or a
            # group-less object reference.  Only the former has no group.
            return False
        return self._root_package_of_import(imp) in self._co_obfuscated

    @staticmethod
    def _resolve_import_package(
        imp: "UnImport", fallback: "UnPackage"
    ) -> Optional["UnPackage"]:
        """Return the loaded package an import points into.

        A super-chain walk crosses package boundaries, so an import met part
        way up may belong to a *dependency* rather than to the package being
        obfuscated.  Its own owner's dependency map is therefore consulted
        first — the primary package need not import (and usually does not
        import) its dependencies' dependencies.

        Args:
            imp (UnImport): The import to resolve.
            fallback (UnPackage): Package whose dependency map to try if the
                import's own owner cannot resolve it.

        Returns:
            Optional[UnPackage]: The loaded package, or None if unknown.
        """
        name = Obfuscator._root_package_of_import(imp)
        if not name:
            return None
        owner = getattr(imp, "package", None)
        if owner is not None:
            found = owner.imported_packages.get(name)
            if found is not None:
                return found
        return fallback.imported_packages.get(name)

    @staticmethod
    def _import_relative_path(imp: "UnImport") -> str:
        """Return an import's path relative to its own source package.

        ``MyMod.MyBaseClass.bEnabled`` becomes
        ``MyBaseClass.bEnabled`` — the form that matches
        :attr:`UnExport.object_name_string` in the package that defines it.

        Args:
            imp (UnImport): The import whose relative path is built.

        Returns:
            str: The dotted path without the leading package name.
        """
        full = imp.object_name_string
        root = Obfuscator._root_package_of_import(imp)
        if root and full.startswith(root + "."):
            return full[len(root) + 1 :]
        return full

    @staticmethod
    def _functions_of_class(pkg: "UnPackage", class_export: "UnExport") -> Set[str]:
        """Return function names directly declared on ``class_export``.

        Args:
            pkg (UnPackage): The package to search.
            class_export (UnExport): The owning class export.

        Returns:
            Set[str]: The names of functions whose group is that class.
        """
        names: Set[str] = set()
        for exp in pkg.exports:
            if (
                exp.class_name_string == "Core.Function"
                and exp.group_item is class_export
            ):
                names.add(exp.object_name.name)
        return names

    # ------------------------------------------------------------------ #
    #  Status book-keeping
    # ------------------------------------------------------------------ #

    def _mark(self, name: Optional["UnName"], status: "ObfuscationStatus") -> None:
        """Record the first reason ``name`` is preserved.

        Subsequent reasons are ignored — the first match wins so the
        printed log shows the strongest justification.

        Args:
            name (Optional[UnName]): The name to record, or ``None`` to
                skip silently.
            status (ObfuscationStatus): The reason the name is preserved.
        """
        if name is None:
            return
        if id(name) in self.name_status:
            return
        self.name_status[id(name)] = (name, status)

    # ------------------------------------------------------------------ #
    #  Name un-sharing (so each definition can be decided independently)
    # ------------------------------------------------------------------ #

    def _collect_name_locked_strings(self, pkg: "UnPackage") -> Set[str]:
        """Return every name string that is referenced by name somewhere.

        A name is "locked" when something resolves it by string rather
        than by an export index: virtual/global/delegate function calls
        and ``NameConst``/label references in bytecode; tagged/default-
        property tag names and struct type names; struct/function/state
        friendly names; enum values; and class metadata (config section,
        package imports, hide categories, property categories).

        Such names must stay shared across all their definitions — e.g.
        an overridden virtual function and every call site must keep the
        same name — so they are excluded from un-sharing. Names reached
        only through export indices (ordinary variable access) are absent
        here and may be split freely.

        Args:
            pkg (UnPackage): The package to scan.

        Returns:
            Set[str]: The set of name strings that must stay shared.
        """
        locked: Set[str] = set()

        def add_index(idx: int) -> None:
            """Add the name at ``idx`` to the locked set if it is valid.

            Args:
                idx (int): The index into the package name table.
            """
            if 0 <= idx < len(pkg.names):
                locked.add(pkg.names[idx].name)

        for export in pkg.exports:
            obj = export.object
            if obj is None:
                continue

            if isinstance(obj, UnStruct) and obj.friendly_name is not None:
                locked.add(obj.friendly_name.name)
            if isinstance(obj, UnEnum):
                for value in obj.names:
                    locked.add(value.name)
            if isinstance(obj, UnProperty) and obj.category_name_entry is not None:
                locked.add(obj.category_name_entry.name)
            if isinstance(obj, UnClass):
                if obj.class_config_name_entry is not None:
                    locked.add(obj.class_config_name_entry.name)
                for n in obj.package_import_names:
                    locked.add(n.name)
                for n in obj.hide_category_names:
                    locked.add(n.name)

            tags = list(getattr(obj, "tagged_properties", []) or [])
            if isinstance(obj, UnClass):
                tags = tags + obj.default_properties
            for tag in tags:
                if tag.tag_name is not None:
                    locked.add(tag.tag_name.name)
                if tag.struct_name_entry is not None:
                    locked.add(tag.struct_name_entry.name)

            parser = getattr(obj, "token_parser", None)
            if parser is not None:
                for token in parser.iter_all_tokens():
                    if isinstance(token, UnTokenNameConst):
                        add_index(token.name_index)
                    elif isinstance(
                        token,
                        (
                            UnTokenVirtualFunction,
                            UnTokenGlobalFunction,
                            UnTokenDelegateFunction,
                            UnTokenDelegateProperty,
                        ),
                    ):
                        add_index(token.function_name)
                    elif isinstance(token, UnTokenLabelTable):
                        for entry in token.entries:
                            add_index(entry.name_index)

        return locked

    def _unshare_object_names(self, pkg: "UnPackage") -> None:
        """Give each variable definition its own name entry.

        A single name string is often shared by many variable definitions
        (e.g. a ``Temp`` local declared in a dozen functions). Sharing
        forces one obfuscation decision for all of them and rewrites them
        to the same symbol. Here we duplicate the shared entry so each
        definition owns a unique :class:`UnName`, letting the rules decide
        each independently and letting the rewriter give each a distinct
        symbol.

        Only names reached purely through export indices are un-shared —
        member variables, function locals, and parameters (properties
        whose owner is a class/state/function, not a plain struct). Names
        that are referenced by string (see
        :meth:`_collect_name_locked_strings`) are left shared so their
        references stay consistent. A later
        :meth:`UnPackage.deduplicate_names` re-merges any copies that end
        up unchanged.

        Args:
            pkg (UnPackage): The package whose names are un-shared.
        """
        name_locked = self._collect_name_locked_strings(pkg)

        groups: Dict[str, List["UnExport"]] = {}
        for export in pkg.exports:
            obj = export.object
            if not isinstance(obj, UnProperty):
                continue
            parent = export.group_item
            parent_obj = parent.object if isinstance(parent, UnExport) else None
            # Skip struct fields: a plain struct's members are addressed by
            # name inside tagged struct data, so they are name-locked.
            if parent_obj is None or type(parent_obj) is UnStruct:
                continue
            groups.setdefault(export.object_name.name, []).append(export)

        for name_str, exports in groups.items():
            if name_str in name_locked or len(exports) <= 1:
                continue
            # Keep every reflection-target definition on the original (shared)
            # entry — those must retain the name a Set/GetPropertyText call looks
            # up by string. If none are reflection targets, keep the first as
            # before. Every other definition gets a fresh copy so each is used
            # exactly once (and can be obfuscated independently).
            protected = {id(e) for e in exports if id(e) in self._reflection_protected}
            if protected:
                movers = [e for e in exports if id(e) not in protected]
            else:
                movers = exports[1:]
            for export in movers:
                original = export.object_name
                copy = UnName(original.name, original.flags)
                pkg.names.append(copy)
                export.object_name = copy

        pkg._invalidate_caches()

    # ------------------------------------------------------------------ #
    #  Exclusion phases
    # ------------------------------------------------------------------ #

    def _exclude_core_references(self, pkg: "UnPackage") -> None:
        """Preserve any name that matches a Core package item (phase 1).

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        core_pkg = pkg.imported_packages.get("Core")
        if core_pkg is None:
            return
        core_item_names: Set[str] = {exp.object_name.name for exp in core_pkg.exports}
        # Also include the object_name of every import that actually
        # lives in Core (i.e. whose group chain root is "Core").
        for imp in pkg.imports:
            if self._root_package_of_import(imp) == "Core":
                core_item_names.add(imp.object_name.name)

        for name_entry in pkg.names:
            if name_entry.name in core_item_names:
                self._mark(name_entry, ObfuscationStatus.CORE_REFERENCE)

    def _exclude_exceptions(self, pkg: "UnPackage", exceptions: List[str]) -> None:
        """Preserve user-supplied exception names (phase 2).

        The match is case-insensitive.

        Args:
            pkg (UnPackage): The package being obfuscated.
            exceptions (List[str]): Names the caller wants left untouched.
        """
        if not exceptions:
            return
        lowered = {e.lower() for e in exceptions}
        for name_entry in pkg.names:
            if name_entry.name.lower() in lowered:
                self._mark(name_entry, ObfuscationStatus.EXCEPTION)

    def _exclude_imports(self, pkg: "UnPackage") -> None:
        """Preserve every name reachable through an import entry (phase 3).

        Imports into a **co-obfuscated** sibling are skipped: that symbol is
        renamed on both sides of the link from one shared symbol map, so
        preserving its name here would merely leak it (see
        :meth:`_is_co_obfuscated_import`).  The sibling's own top-level
        ``Package`` entry — its file name — is still preserved.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for imp in pkg.imports:
            if not self._is_co_obfuscated_import(imp):
                self._mark(imp.object_name, ObfuscationStatus.IMPORT_REFERENCE)
            self._mark(imp.class_name, ObfuscationStatus.IMPORT_REFERENCE)
            self._mark(imp.class_package_name, ObfuscationStatus.IMPORT_REFERENCE)
            # Walk the group chain (an import may live inside another import).
            # A group that is itself a co-obfuscated reference (the owning
            # class of an imported member) is skipped for the same reason;
            # the chain's root package entry always has no group, so it is
            # always preserved.
            gi: Optional["UnPackageItem"] = imp.group_item
            while gi is not None:
                if not (isinstance(gi, UnImport) and self._is_co_obfuscated_import(gi)):
                    self._mark(gi.object_name, ObfuscationStatus.IMPORT_REFERENCE)
                gi = gi.group_item

    def _exclude_external_superclasses(self, pkg: "UnPackage") -> None:
        """Preserve class names that inherit a config/localized section (phase 4).

        A class must keep its original name when it (or an ancestor) causes an
        INI section **keyed on this class's name** to exist, because that lookup
        is by string and would break under renaming. That happens when the class
        inherits a plain ``config`` property: ``config`` values are stored
        per-most-derived-class, so the subclass owns an ``[Package.Subclass]``
        section named after itself.

        Notes:

        * The class's **own** config/localized/globalconfig properties are already
          handled by :meth:`_exclude_config_localized` (which preserves the owning
          class), so this phase only walks **ancestors** (local and external).
        * ``globalconfig`` **and** ``localized`` are deliberately **excluded** from
          the inherited set: both are stored under the class that *declares* them
          (the INI/`.int` section is keyed on the declaring class, not on an
          inheriting subclass), so inheriting one does not pin the subclass name.
          This is what lets leaf classes with no ``config`` of their own — e.g.
          ``VersionInfo`` (whose only config-ish inheritance is ``Info``'s
          *localized* groups) — be obfuscated.
        * A struct whose super chain reaches a **co-obfuscated** sibling is not
          preserved: the type name is renamed identically in both packages.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        inherit_mask = int(UnPropertyFlags.Config)
        for export in pkg.exports:
            # Structs keep the original rule: a local struct whose super chain
            # reaches a dependency is preserved (its type name may be referenced
            # from outside). Structs carry no config, so the class rule below
            # does not apply to them.
            if export.class_name_string == "Core.Struct":
                s: Optional["UnPackageItem"] = export.super_item
                while s is not None:
                    if isinstance(s, UnImport):
                        if not self._is_co_obfuscated_import(s):
                            self._mark(
                                export.object_name,
                                ObfuscationStatus.EXTERNAL_SUPERCLASS,
                            )
                        break
                    if isinstance(s, UnExport):
                        s = s.super_item
                    else:
                        break
                continue
            if export.class_name_string != "Core.Class":
                continue

            cur_pkg: "UnPackage" = pkg
            cur_super: Optional["UnPackageItem"] = export.super_item
            preserve = False

            while cur_super is not None:
                if isinstance(cur_super, UnImport):
                    ext_pkg_name = self._root_package_of_import(cur_super)
                    ext_pkg = self._resolve_import_package(cur_super, pkg)
                    if ext_pkg is None:
                        # Can't inspect the dependency — preserve to be safe.
                        preserve = True
                        break
                    ext_class = self._find_class_export(
                        ext_pkg, cur_super.object_name.name
                    )
                    if ext_class is None:
                        break
                    if self._class_declares_property(ext_pkg, ext_class, inherit_mask):
                        preserve = True
                        break
                    cur_pkg = ext_pkg
                    cur_super = ext_class.super_item
                elif isinstance(cur_super, UnExport):
                    if self._class_declares_property(cur_pkg, cur_super, inherit_mask):
                        preserve = True
                        break
                    cur_super = cur_super.super_item
                else:
                    break

            if preserve:
                self._mark(
                    export.object_name,
                    ObfuscationStatus.CONFIG_INHERITED_CLASS,
                )

    @staticmethod
    def _class_declares_property(
        pkg: "UnPackage", class_export: "UnExport", flag_mask: int
    ) -> bool:
        """Return True if ``class_export`` directly declares a matching property.

        Args:
            pkg (UnPackage): The package that owns ``class_export``.
            class_export (UnExport): The class whose own properties are checked.
            flag_mask (int): Property-flag bits; any property whose flags
                intersect this mask counts as a match.

        Returns:
            bool: True if a directly-declared property matches ``flag_mask``.
        """
        for exp in pkg.exports:
            if exp.group_item is not class_export:
                continue
            obj = exp.object
            if isinstance(obj, UnProperty) and (obj.property_flags & flag_mask):
                return True
        return False

    def _exclude_function_overrides(self, pkg: "UnPackage") -> None:
        """Preserve functions that override an external function (phase 5).

        For every ``Core.Function`` export we walk the parent class's
        super chain. Once the chain leaves this package (an
        :class:`UnImport`), we cross into the dependency package and check
        whether the external class (and its own ancestors) declares a
        function with the same name. If so, the local function name must
        remain stable.

        A match inside a **co-obfuscated** sibling does not preserve the
        name — the overridden function is renamed there to the same shared
        token, so the override tracks it — but the walk continues past that
        class, because an ancestor further up (in the engine) may still
        declare the function and pin the name.

        Args:
            pkg (UnPackage): The package being obfuscated.

        Raises:
            RuntimeError: If an external superclass's dependency package
                is not loaded and cannot be resolved.
        """
        for export in pkg.exports:
            if export.class_name_string != "Core.Function":
                continue
            parent_class = export.group_item
            if not isinstance(parent_class, UnExport):
                continue
            func_name = export.object_name.name

            cur_pkg: "UnPackage" = pkg
            cur_super: Optional["UnPackageItem"] = parent_class.super_item
            crossed_external = False
            # True while the walk is inside a co-obfuscated sibling, whose
            # declarations do not pin our name.
            in_co_obfuscated = False

            while cur_super is not None:
                if isinstance(cur_super, UnImport):
                    ext_pkg_name = self._root_package_of_import(cur_super)
                    ext_pkg = self._resolve_import_package(cur_super, pkg)
                    if ext_pkg is None:
                        raise RuntimeError(
                            f"Dependency package {ext_pkg_name!r} is not "
                            f"loaded; cannot resolve external superclass "
                            f"{cur_super.object_name.name!r} for function "
                            f"{export.object_name_string!r}"
                        )
                    ext_class = self._find_class_export(
                        ext_pkg, cur_super.object_name.name
                    )
                    if ext_class is None:
                        break
                    in_co_obfuscated = ext_pkg_name in self._co_obfuscated
                    if not in_co_obfuscated and func_name in self._functions_of_class(
                        ext_pkg, ext_class
                    ):
                        self._mark(
                            export.object_name,
                            ObfuscationStatus.EXTERNAL_FUNCTION_OVERRIDE,
                        )
                        break
                    cur_pkg = ext_pkg
                    cur_super = ext_class.super_item
                    crossed_external = True
                elif isinstance(cur_super, UnExport):
                    if crossed_external and not in_co_obfuscated:
                        # We are inside the external package — also test
                        # this internal-to-the-external-package class.
                        if func_name in self._functions_of_class(cur_pkg, cur_super):
                            self._mark(
                                export.object_name,
                                ObfuscationStatus.EXTERNAL_FUNCTION_OVERRIDE,
                            )
                            break
                    cur_super = cur_super.super_item
                else:
                    break

    def _exclude_external_function_names(self, pkg: "UnPackage") -> None:
        """Preserve names matching any function declared in a dependency.

        Tokenised bytecode may call external functions by name (e.g.
        ``VirtualFunction`` opcodes carry the function name as an FName
        index into the local name table). When the called function lives
        in a dependency package, its name must not be rewritten —
        otherwise the call site would target a non-existent symbol at
        runtime.

        This is a name-level (string) match across every function export
        in every loaded dependency package. It's a deliberately wide net
        because we can't statically know which call sites use which names,
        and the cost of being too cautious is just slightly less
        obfuscation.

        Dependencies obfuscated in the **same run** are excluded from the
        net: their function names are being rewritten too, and every
        co-obfuscated package draws the replacement from one shared symbol
        map, so a call site tracks the rename.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        external_func_names: Set[str] = set()
        for dep_name, dep_pkg in pkg.imported_packages.items():
            if dep_name in self._co_obfuscated:
                continue
            for exp in dep_pkg.exports:
                if exp.class_name_string == "Core.Function":
                    external_func_names.add(exp.object_name.name)
        if not external_func_names:
            return
        for name_entry in pkg.names:
            if name_entry.name in external_func_names:
                self._mark(name_entry, ObfuscationStatus.EXTERNAL_FUNCTION_NAME)

    def _exclude_config_localized(self, pkg: "UnPackage") -> None:
        """Preserve names tied to Config/Localized properties (phase 6).

        For every property export marked ``CPF_Config`` or
        ``CPF_Localized``:

        * The property's own name is preserved.
        * The owning class's name is preserved (the INI/Loc lookup is
          keyed on it).
        * The class's :attr:`UnClass.class_config_name_entry` (the INI
          section name) is preserved.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        config_localized_mask = int(
            UnPropertyFlags.Config
            | UnPropertyFlags.Localized
            | UnPropertyFlags.GlobalConfig
        )

        for export in pkg.exports:
            obj = export.object
            if not isinstance(obj, UnProperty):
                continue
            if not (obj.property_flags & config_localized_mask):
                continue

            # Property name itself.
            self._mark(
                export.object_name,
                ObfuscationStatus.CONFIG_LOCALIZED_PROPERTY,
            )

            # Owning class.
            owner = export.group_item
            if isinstance(owner, UnExport):
                self._mark(
                    owner.object_name,
                    ObfuscationStatus.CONFIG_LOCALIZED_CLASS,
                )
                owner_obj = owner.object
                if isinstance(owner_obj, UnClass):
                    self._mark(
                        owner_obj.class_config_name_entry,
                        ObfuscationStatus.CONFIG_LOCALIZED_INI,
                    )

    def _exclude_native(self, pkg: "UnPackage") -> None:
        """Preserve names of anything marked ``Native`` (phase 7).

        Native code in the engine refers to these symbols by their
        original name, so the rewriter must leave them alone. This
        covers:

        * Exports whose ``UnObjectFlags.Native`` bit is set.
        * Functions whose ``UnFunctionFlags.Native`` bit is set.
        * Structs whose ``UnStructFlags.Native`` bit is set.
        * Properties whose ``UnPropertyFlags.Native`` bit is set (and the
          property's owning class).

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            obj = export.object
            is_native = bool(export.flags & UnObjectFlags.Native)

            if isinstance(obj, UnFunction):
                if obj.function_flags & UnFunctionFlags.Native:
                    is_native = True
            elif isinstance(obj, UnProperty):
                if obj.property_flags & int(UnPropertyFlags.Native):
                    is_native = True
                    # The owning class must also be preserved so native
                    # code can resolve the property lookup.
                    owner = export.group_item
                    if isinstance(owner, UnExport):
                        self._mark(
                            owner.object_name,
                            ObfuscationStatus.NATIVE,
                        )
            elif isinstance(obj, UnStruct):
                # UnStruct is the base for UnState/UnClass/UnFunction;
                # the subclass branches above cover their specific
                # flags, but UnStruct's own Native flag applies to
                # plain struct definitions.
                if obj.struct_flags & int(UnStructFlags.Native):
                    is_native = True

            if is_native:
                self._mark(export.object_name, ObfuscationStatus.NATIVE)

    def _exclude_native_class_variables(self, pkg: "UnPackage") -> None:
        """Preserve every member variable of a native class (phase 7b).

        A native class has an engine (native) counterpart whose code reaches
        the class's ``UProperty`` members by name (property lookup /
        ``FindField``), so renaming any member variable of a native class
        would break that access. Every ``UnProperty`` directly declared
        on a native class (``UnObjectFlags.Native`` on the class export)
        is preserved — not just those individually flagged ``Native``.
        Function locals and parameters live under the function, not the
        class, so they are not affected.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            obj = export.object
            if not (isinstance(obj, UnClass) and export.flags & UnObjectFlags.Native):
                continue
            child = getattr(obj, "children", None)
            while child is not None:
                child_obj = child.object
                if isinstance(child_obj, UnProperty):
                    self._mark(
                        child.object_name,
                        ObfuscationStatus.NATIVE_CLASS_VARIABLE,
                    )
                child = child_obj.next_item if isinstance(child_obj, UnField) else None

    def _exclude_functions_invoked_by_name(self, pkg: "UnPackage") -> None:
        """Preserve functions the engine reaches by name (phase 8).

        Most calls compile to a reference the rewriter carries along, but
        a few kinds of function are located by their name at run time, so
        renaming them severs the call:

        * ``Exec`` — console commands typed by the player.
        * ``Event`` — events the engine dispatches by name.
        * ``Operator`` / ``PreOperator`` — resolved through the operator
          token.

        (``Net`` / ``Static`` / ``Delegate`` functions are not preserved:
        they are referenced by compiled index and are safe to rename.)

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        preserve_mask = (
            UnFunctionFlags.Exec
            | UnFunctionFlags.Event
            | UnFunctionFlags.Operator
            | UnFunctionFlags.PreOperator
        )
        for export in pkg.exports:
            obj = export.object
            if isinstance(obj, UnFunction) and (obj.function_flags & preserve_mask):
                self._mark(
                    export.object_name,
                    ObfuscationStatus.FUNCTION_INVOKED_BY_NAME,
                )

    def _exclude_commandlet_classes(self, pkg: "UnPackage") -> None:
        """Preserve the name of every ``Commandlet`` subclass (phase 3b).

        A commandlet is launched as ``ucc <Package>.<Class>``, so the engine
        resolves its class purely by string off the command line: renaming it
        makes the commandlet unreachable (``ucc`` reports it as *not found*).
        Every class whose super chain reaches ``Commandlet`` therefore keeps
        its name — including one in a *dependency*, so a chain that leaves this
        package is followed across the import.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            if export.class_name_string != "Core.Class":
                continue
            cur: Optional["UnPackageItem"] = export.super_item
            # Bounded walk: a malformed chain must not spin here.
            for _ in range(64):
                if cur is None:
                    break
                name = cur.object_name.name if cur.object_name is not None else ""
                if name == "Commandlet":
                    self._mark(
                        export.object_name,
                        ObfuscationStatus.COMMANDLET_CLASS,
                    )
                    break
                if isinstance(cur, UnExport):
                    cur = cur.super_item
                elif isinstance(cur, UnImport):
                    ext_pkg = self._resolve_import_package(cur, pkg)
                    ext_class = (
                        self._find_class_export(ext_pkg, name)
                        if ext_pkg is not None
                        else None
                    )
                    cur = ext_class.super_item if ext_class is not None else None
                else:
                    break

    def _exclude_state_names(self, pkg: "UnPackage") -> None:
        """Preserve every state name (phase 9).

        States are frequently entered by name rather than by a compiled
        reference — ``GotoState('SomeState')`` may be called with a string
        or computed name, and native code can push states by name — so a
        renamed state could no longer be reached. Preserving all state
        names is the safe choice; it costs a little obfuscation coverage.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            if export.class_name_string == "Core.State":
                self._mark(export.object_name, ObfuscationStatus.STATE_NAME)

    def _exclude_stringified_names(self, pkg: "UnPackage") -> None:
        """Preserve name literals observed as a string (phase 10).

        When bytecode casts a name to a string (``string(SomeName)``, or
        a comparison that forces the cast), the text of that name becomes
        observable — it may be printed, logged, or compared against a
        string literal. Renaming such a name would change the observed
        value and break that logic, so any ``NameConst`` fed into a
        ``NameToString`` cast is preserved. (Casts of name variables carry
        no single literal to protect.)

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            obj = export.object
            parser = getattr(obj, "token_parser", None)
            if parser is None:
                continue
            for token in parser.iter_all_tokens():
                if (
                    isinstance(token, UnTokenPrimitiveCast)
                    and token.cast_type == int(UnCastType.NameToString)
                    and isinstance(token.expression, UnTokenNameConst)
                ):
                    idx = token.expression.name_index
                    if 0 <= idx < len(pkg.names):
                        self._mark(
                            pkg.names[idx],
                            ObfuscationStatus.STRINGIFIED_NAME,
                        )

    def _referenced_property(
        self, pkg: "UnPackage", token: Optional[object]
    ) -> Optional[object]:
        """Resolve the property an expression token ultimately reads.

        Unwraps context / struct-member / cast wrappers to find the leaf
        variable reference and returns its resolved property object (or None).

        Args:
            pkg (UnPackage): The package being obfuscated.
            token (Optional[object]): The expression token to inspect.

        Returns:
            Optional[object]: The referenced property object, or None.
        """
        cur = token
        for _ in range(16):  # bounded walk through nested wrappers
            if cur is None:
                return None
            ref = getattr(cur, "object_ref", None) or getattr(cur, "property_ref", None)
            if ref:
                item = resolve_item(pkg, ref)
                return item.object if item is not None else None
            cur = (
                getattr(cur, "context_expr", None)
                or getattr(cur, "inner_expr", None)
                or getattr(cur, "expression", None)
            )
        return None

    def _exclude_stringified_enums(self, pkg: "UnPackage") -> None:
        """Preserve an entire enum whose values are cast to a string.

        Casting an enum value to a string (``string(SomeEnumVar)``) yields the
        enum member's *name* at runtime, so renaming either the enum type or any
        of its value names would change the observed text. When bytecode casts an
        enum-typed byte to string (a ``ByteToString`` cast over a reference to an
        enum-typed ``UnByteProperty``), the enum's type-name entry is preserved —
        which, because :meth:`_obfuscate_enum_values` skips any enum whose type is
        already preserved, keeps the values readable too.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            obj = export.object
            parser = getattr(obj, "token_parser", None)
            if parser is None:
                continue
            for token in parser.iter_all_tokens():
                if not (
                    isinstance(token, UnTokenPrimitiveCast)
                    and token.cast_type == int(UnCastType.ByteToString)
                ):
                    continue
                prop = self._referenced_property(pkg, token.expression)
                if not isinstance(prop, UnByteProperty):
                    continue
                if isinstance(prop.enum_item, UnExport):
                    self._mark(
                        prop.enum_item.object_name,
                        ObfuscationStatus.STRINGIFIED_ENUM,
                    )

    # ------------------------------------------------------------------ #
    #  Serialized object data (phase 11)
    # ------------------------------------------------------------------ #

    def _object_data_tag_groups(
        self, export: "UnExport"
    ) -> List[Tuple[Optional["UnPackageItem"], List[Any]]]:
        """Return an export's tagged-property streams with their owning class.

        Every serialized object stores its non-default values as a *tagged*
        stream: each value is prefixed by the name of the property it sets. The
        owning class is the one that must resolve those names — the export's own
        class for a class's ``defaultproperties``, the instance's class for a
        content object / embedded component.

        A content export whose class has no dedicated parser (an emitter, a GUI
        component) is left with ``object is None`` by the loader so its raw bytes
        round-trip untouched; its tags are read with a throwaway
        :class:`UnDefaultObject`, which never touches ``export.object``.

        Args:
            export (UnExport): The export whose stored data is examined.

        Returns:
            List[Tuple[Optional[UnPackageItem], List[Any]]]: ``(owning class,
                tags)`` pairs; the owning class may be an import (foreign class)
                or None (unresolved).
        """
        obj = export.object
        if obj is None:
            if not getattr(export, "export_data", None):
                return []
            probe = UnDefaultObject(export)
            try:
                probe.parse()
            except Exception:  # pragma: no cover - defensive
                return []
            return [(export.class_item, list(probe.tagged_properties))]

        groups: List[Tuple[Optional["UnPackageItem"], List[Any]]] = []
        inst = list(getattr(obj, "tagged_properties", None) or [])
        if inst:
            groups.append((export.class_item, inst))
        if isinstance(obj, UnClass) and obj.default_properties:
            groups.append((export, list(obj.default_properties)))
        return groups

    def _collect_stream_names(
        self,
        pkg: "UnPackage",
        buf: "io.BytesIO",
        out: List["UnName"],
        parent_struct: str = "",
    ) -> None:
        """Collect the member-name entries of one tagged struct-data stream.

        Reads exactly one stream (up to and including its ``None`` terminator)
        from the current position, so a caller can walk the elements of an
        array-of-struct blob back to back. Descends into a nested struct value
        and into the elements of a nested array of structs — each of those is a
        tagged stream too, and every name in it is resolved by string.

        Args:
            pkg (UnPackage): The package the name indices refer into.
            buf (io.BytesIO): The buffer positioned at the start of a stream.
            out (List[UnName]): Accumulator for every name entry met.
            parent_struct (str): Name of the struct whose members this stream
                sets, used to scope an array member's inner-type lookup.
                Defaults to "".

        Raises:
            ValueError: If the data does not read as a tagged stream (the caller
                then discards everything it collected).
        """
        size = len(buf.getbuffer())
        while True:
            if buf.tell() >= size:
                raise ValueError("truncated tagged stream")
            idx = read_index(buf)
            if not 0 <= idx < len(pkg.names):
                raise ValueError("name index out of range")
            entry = pkg.names[idx]
            if entry.name == "None":
                return
            tag = UnPropertyTag()
            tag.name_index = idx
            tag.tag_name = entry
            tag.parse(buf, package=pkg)
            out.append(entry)
            if tag.struct_name_entry is None:
                if tag.type == int(UnNameMap.ArrayProperty) and tag.property_data:
                    self._collect_array_element_names(pkg, tag, out, parent_struct)
                continue
            out.append(tag.struct_name_entry)
            if tag.property_data and not _is_native_serialize_struct(
                tag.struct_name_entry.name
            ):
                self._collect_stream_names(
                    pkg,
                    io.BytesIO(tag.property_data),
                    out,
                    tag.struct_name_entry.name,
                )

    def _collect_array_element_names(
        self,
        pkg: "UnPackage",
        tag: Any,
        out: List["UnName"],
        parent_struct: str = "",
    ) -> None:
        """Collect the member names of an array-of-struct value's elements.

        An array's inner type is not in its tag, so the property is resolved to
        find it; a non-struct (or native-serialize) inner type carries no names
        and is skipped.

        Args:
            pkg (UnPackage): The package the name indices refer into.
            tag (UnPropertyTag): The array tag whose data is walked.
            out (List[UnName]): Accumulator for every name entry met.
            parent_struct (str): Struct that declares the array member, used to
                scope the lookup. Defaults to "".

        Raises:
            ValueError: If the data does not read as tagged elements.
        """
        if tag.tag_name is None:
            return
        _, struct_ref = _find_array_inner_info(
            tag.tag_name.name, pkg, parent_struct_name=parent_struct
        )
        if not struct_ref:
            return
        inner_name = struct_ref.split(".")[-1]
        if _is_native_serialize_struct(inner_name):
            return
        buf = io.BytesIO(tag.property_data)
        count = read_index(buf)
        if count < 0:
            raise ValueError("negative element count")
        for _ in range(count):
            self._collect_stream_names(pkg, buf, out, inner_name)

    def _struct_member_name_entries(self, pkg: "UnPackage", tag: Any) -> List["UnName"]:
        """Return the member names buried in a struct-typed value's data.

        A struct-typed value is itself a tagged stream, so a ``RangeVector``
        default on an emitter addresses ``X``/``Y``/``Z`` — and each of those a
        ``Min``/``Max`` — by name; an array of structs (``SizeScale``) is a count
        followed by one such stream per element. ``Vector``/``Rotator``/``Color``
        are positional and carry no names, so those are skipped: reading one as a
        stream would yield nonsense.

        Says nothing about whose struct it is — see
        :meth:`_foreign_struct_member_names` for the names that must be
        *preserved*, and :meth:`_stored_stream_strings` for the ones that must
        merely rename consistently family-wide.

        Args:
            pkg (UnPackage): The package being obfuscated.
            tag (UnPropertyTag): The tag whose value data is examined.

        Returns:
            List[UnName]: The name entries met, or [] if the inner type is not a
                tagged struct or the blob does not read as one (nothing is
                assumed about data we cannot parse).
        """
        data = tag.value_data(pkg)
        if not data:
            return []
        entry = tag.struct_name_entry
        if entry is not None:
            inner_name: str = entry.name
            elements = 1
        elif tag.type == int(UnNameMap.ArrayProperty) and tag.tag_name is not None:
            # An array's inner type is not in the tag: resolve the property
            # itself (a foreign one resolves through the dependency packages).
            _, struct_ref = _find_array_inner_info(tag.tag_name.name, pkg)
            if not struct_ref:
                return []
            inner_name = struct_ref.split(".")[-1]
            elements = -1  # count-prefixed
        else:
            return []
        if _is_native_serialize_struct(inner_name):
            return []
        out: List["UnName"] = []
        try:
            buf = io.BytesIO(data)
            if elements < 0:
                elements = read_index(buf)
                if elements < 0:
                    raise ValueError("negative element count")
            for _ in range(elements):
                self._collect_stream_names(pkg, buf, out, inner_name)
        except Exception:
            return []
        return out

    def _struct_inner_type_name(self, pkg: "UnPackage", tag: Any) -> str:
        """Return the struct type name a tag's value data is written against.

        Args:
            pkg (UnPackage): The package being obfuscated.
            tag (UnPropertyTag): The tag to classify.

        Returns:
            str: The struct's short name, or ``""`` when the tag is not
                struct-typed (or its inner type cannot be resolved).
        """
        if tag.struct_name_entry is not None:
            return str(tag.struct_name_entry.name)
        if tag.type == int(UnNameMap.ArrayProperty) and tag.tag_name is not None:
            _, struct_ref = _find_array_inner_info(tag.tag_name.name, pkg)
            if struct_ref:
                return struct_ref.split(".")[-1]
        return ""

    def _foreign_struct_member_names(
        self, pkg: "UnPackage", tag: Any
    ) -> List["UnName"]:
        """Return the member names of a FOREIGN struct value's data.

        Local structs are excluded: their members are renamed in lockstep with
        the tag data by :meth:`_repoint_tag_data`. What is left belongs to a
        struct this package does not declare, so it has to keep reading as it
        does — including a struct a co-obfuscated sibling declares, whose
        definition is preserved for us by :meth:`_exclude_sibling_data_tags`.

        Args:
            pkg (UnPackage): The package being obfuscated.
            tag (UnPropertyTag): The tag whose value data is examined.

        Returns:
            List[UnName]: The name entries to preserve, or [] when the struct is
                local (or the value is not a readable tagged struct).
        """
        inner_name = self._struct_inner_type_name(pkg, tag)
        if not inner_name or self._find_class_export(pkg, inner_name) is not None:
            return []
        return self._struct_member_name_entries(pkg, tag)

    def _declares_property(
        self,
        pkg: "UnPackage",
        owner: Optional["UnPackageItem"],
        name: str,
        cache: Dict[int, Dict[str, "UnExport"]],
    ) -> bool:
        """Whether ``owner``'s local class chain declares property ``name``.

        The walk stops at the first import: a class in another package declares
        its own properties, and this package's rename does not reach them.

        Args:
            pkg (UnPackage): The package to resolve exports in.
            owner (Optional[UnPackageItem]): The class the tag resolves in.
            name (str): The property name to look for.
            cache (Dict[int, Dict[str, UnExport]]): Per-class property cache,
                shared across calls.

        Returns:
            bool: True when a locally-defined class in the chain declares it.
        """
        cur = owner
        while isinstance(cur, UnExport):
            props = cache.get(id(cur))
            if props is None:
                props = self._own_properties(pkg, cur)
                cache[id(cur)] = props
            if name in props:
                return True
            cur = cur.super_item
        return False

    def _sibling_data_tag_names(self, packages: List["UnPackage"]) -> Set[str]:
        """Return the names a sibling's stored object data addresses.

        A subclass storing a default for a property its SUPERCLASS declares one
        package over addresses that property by name, in its own default-object
        stream, with no import to speak for the link (``MyModPlus``'s
        ``MyDerivedClass`` overriding one ``MySettings`` entry of
        ``MyMod``'s ``MyBaseClass``). Both ends must agree, and the
        only end that can be made to move is neither: the referencing package
        preserves the tag (:meth:`_exclude_object_data_tags`), so the DECLARING
        package has to preserve the definition. Otherwise the engine finds no
        property of that name on the (renamed) superclass and drops the value in
        **silence**, leaving the inherited default in place — a corrupted
        default that loads perfectly happily.

        Renaming both ends instead would need the declaring package's token
        threaded into a *fresh* name entry in the referencing package (its own
        entry may be shared with, and preserved for, an unrelated reference).
        Preserving is a handful of names; the tokens are not worth it.

        Only a tag the referencing package does not declare itself counts — a
        local one is renamed together with its data. Which package declares the
        name is then matched family-wide rather than resolved through the class
        chain, so this can over-collect; the cost of a false positive is one
        name kept readable.

        Must be called **before** any package in the family is rewritten.

        Args:
            packages (List[UnPackage]): The family being obfuscated.

        Returns:
            Set[str]: Names every package in the family must leave readable.
        """
        declared: Dict[str, Set[str]] = {
            self._simple_name(pkg): {
                exp.object_name.name
                for exp in pkg.exports
                if isinstance(exp.object, UnProperty)
            }
            for pkg in packages
        }
        names: Set[str] = set()
        for pkg in packages:
            me = self._simple_name(pkg)
            elsewhere = {
                name
                for other, props in declared.items()
                if other != me
                for name in props
            }
            if not elsewhere:
                continue
            cache: Dict[int, Dict[str, "UnExport"]] = {}
            for export in pkg.exports:
                for owner, tags in self._object_data_tag_groups(export):
                    for tag in tags:
                        if tag.tag_name is None or tag.tag_name.name not in elsewhere:
                            continue
                        if self._declares_property(
                            pkg, owner, tag.tag_name.name, cache
                        ):
                            continue
                        names.add(tag.tag_name.name)
                        # The value's own stream names the struct and its
                        # members, which live with the property.
                        if tag.struct_name_entry is not None:
                            names.add(tag.struct_name_entry.name)
                        names.update(
                            entry.name
                            for entry in self._struct_member_name_entries(pkg, tag)
                        )
        return names

    def _exclude_sibling_data_tags(
        self, pkg: "UnPackage", names: Optional[Set[str]]
    ) -> None:
        """Preserve the names a sibling's stored data addresses (phase 1b).

        Runs before every other exclusion so the mark wins the first-write-wins
        race, and deliberately carries a status that is **not** a leak status: a
        leak status would let :meth:`_split_shadowed_properties` move the local
        definition to a fresh entry and obfuscate it, which is exactly the rename
        that has to not happen here.

        Args:
            pkg (UnPackage): The package being obfuscated.
            names (Optional[Set[str]]): Names from
                :meth:`_sibling_data_tag_names`.
        """
        if not names:
            return
        for entry in pkg.names:
            if entry.name in names:
                self._mark(entry, ObfuscationStatus.SIBLING_DATA_TAG)

    def _exclude_object_data_tags(self, pkg: "UnPackage") -> None:
        """Preserve every property name a stored object's data addresses (phase 11).

        A content object — a Texture, Sound, Shader, FinalBlend, an emitter, an
        embedded GUI component — stores its values as a tagged stream keyed by
        the *name* of the engine property each one sets. Those names belong to a
        foreign (engine) class, which resolves them by string when the object is
        loaded, so renaming one silently breaks the object: obfuscating
        a ``Date`` function's ``Format`` parameter renamed the shared name
        entry, and with it the ``ConsoleBox`` texture's ``Engine.Bitmap.Format``
        tag — a corrupt pixel format on the console background.

        The same holds for a *local* class's ``defaultproperties`` tag that names
        an inherited engine property: only a tag naming a property this package
        declares itself may be renamed (that rename is applied to the definition
        and the tag in lockstep, through the one shared name entry).

        Marking here does not cost obfuscation coverage: the status is one of the
        leak statuses in :meth:`_split_shadowed_properties`, so a local
        definition that merely *shares* the name gets its own entry and is
        obfuscated, while the stored tag keeps the readable original.

        A tag naming a property a co-obfuscated SIBLING declares is preserved
        here as well, and :meth:`_exclude_sibling_data_tags` makes that sibling
        keep the definition readable to match — see there for why the link is
        kept by preserving both ends rather than renaming both.

        A name an import into a sibling resolves by string is skipped: that one
        must follow the sibling's rename (see :meth:`_co_import_name_ids`).

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        linked = self._co_import_name_ids(pkg)
        prop_cache: Dict[int, Dict[str, "UnExport"]] = {}

        def preserve(entry: Optional["UnName"]) -> None:
            """Mark ``entry`` as addressed by stored object data.

            Args:
                entry (Optional[UnName]): The name entry to preserve.
            """
            if entry is not None and id(entry) not in linked:
                self._mark(entry, ObfuscationStatus.OBJECT_DATA_TAG)

        for export in pkg.exports:
            for owner, tags in self._object_data_tag_groups(export):
                for tag in tags:
                    if tag.tag_name is not None and not self._declares_property(
                        pkg, owner, tag.tag_name.name, prop_cache
                    ):
                        preserve(tag.tag_name)
                    if (
                        tag.struct_name_entry is not None
                        and self._find_class_export(pkg, tag.struct_name_entry.name)
                        is None
                    ):
                        preserve(tag.struct_name_entry)
                    for entry in self._foreign_struct_member_names(pkg, tag):
                        preserve(entry)

    # ------------------------------------------------------------------ #
    #  Shadowed-definition splitting (phase 12)
    # ------------------------------------------------------------------ #

    def _reflection_protected_property_ids(self, pkg: "UnPackage") -> Set[int]:
        """Return id() of property exports looked up by string via reflection.

        ``SetPropertyText("Name", …)`` / ``GetPropertyText("Name")`` resolve a
        property by the literal name at runtime, so that specific property must
        keep its name. The lookup is resolved in the *calling function's class*
        (the common ``self.SetPropertyText(...)`` case): the literal is matched
        against that class's own-or-inherited property named ``Name``. This is
        precise — only the property the self-call targets is protected, so an
        unrelated same-named property in another class (e.g. one of the several
        ``Player`` fields) is still obfuscated. Only the *first* argument of the
        two native calls is treated as a property name.

        Args:
            pkg (UnPackage): The package being obfuscated.

        Returns:
            Set[int]: id() of property exports that must not be obfuscated.
        """
        reflection = {"SetPropertyText", "GetPropertyText"}
        # class-export -> set of property names it looks up via reflection
        calls: Dict[int, Tuple["UnExport", Set[str]]] = {}
        for export in pkg.exports:
            parser = getattr(export.object, "token_parser", None)
            if parser is None:
                continue
            # the class that owns this function/state export
            cls = export.group_item
            while isinstance(cls, UnExport) and not isinstance(cls.object, UnClass):
                cls = cls.group_item
            if not isinstance(cls, UnExport):
                continue
            for token in parser.iter_all_tokens():
                fn_name = None
                if isinstance(token, UnTokenFinalFunction):
                    item = resolve_item(pkg, token.function_ref)
                    if item is not None:
                        fn_name = item.object_name.name
                elif isinstance(
                    token,
                    (
                        UnTokenVirtualFunction,
                        UnTokenGlobalFunction,
                        UnTokenDelegateFunction,
                    ),
                ):
                    idx = getattr(token, "function_name", -1)
                    if 0 <= idx < len(pkg.names):
                        fn_name = pkg.names[idx].name
                if fn_name not in reflection:
                    continue
                params = getattr(token, "params", None) or []
                if params and isinstance(params[0], UnTokenStringConst):
                    calls.setdefault(id(cls), (cls, set()))[1].add(params[0].value)

        # Resolve each looked-up name in its calling class's own+inherited props.
        protected_ids: Set[int] = set()
        prop_cache: Dict[int, Dict[str, "UnExport"]] = {}

        def own_props(cls_export):
            props = prop_cache.get(id(cls_export))
            if props is None:
                props = self._own_properties(pkg, cls_export)
                prop_cache[id(cls_export)] = props
            return props

        for cls, names in calls.values():
            for name in names:
                cur: Optional["UnPackageItem"] = cls
                while isinstance(cur, UnExport):
                    prop = own_props(cur).get(name)
                    if prop is not None:
                        protected_ids.add(id(prop))
                        break
                    cur = cur.super_item
        return protected_ids

    def _structs_with_member_defaults(self, pkg: "UnPackage") -> Set[str]:
        """Return names of struct types that carry compiled member defaults.

        A ``StructProperty`` tag — or an ``ArrayProperty`` whose inner type is a
        struct — with non-empty data encodes member values by name index inside
        its raw bytes. Those nested references are NOT re-linked by
        :meth:`_split_shadowed_properties` (only top-level tags are), so a struct
        field is left un-split when its struct type appears with such a default —
        renaming the field would strand the nested reference.

        Args:
            pkg (UnPackage): The package being obfuscated.

        Returns:
            Set[str]: Struct type names to leave untouched.
        """
        names: Set[str] = set()
        for export in pkg.exports:
            obj = export.object
            tags = list(getattr(obj, "tagged_properties", []) or [])
            if isinstance(obj, UnClass):
                tags += obj.default_properties
            for tag in tags:
                if not tag.property_data:
                    continue
                if tag.struct_name_entry is not None:
                    names.add(tag.struct_name_entry.name)
                    continue
                # ArrayProperty whose inner type is a struct also embeds member
                # names in each element; flag that inner struct too.
                if tag.tag_name is not None:
                    inner_type, struct_ref = _find_array_inner_info(
                        tag.tag_name.name, pkg
                    )
                    if struct_ref:
                        names.add(struct_ref.split(".")[-1])
        return names

    @staticmethod
    def _own_properties(
        pkg: "UnPackage", class_export: "UnExport"
    ) -> Dict[str, "UnExport"]:
        """Map name -> property export for properties directly owned by a class.

        Args:
            pkg (UnPackage): The package to search.
            class_export (UnExport): The owning class/state/struct export.

        Returns:
            Dict[str, UnExport]: The directly-declared property exports by name.
        """
        return {
            exp.object_name.name: exp
            for exp in pkg.exports
            if exp.group_item is class_export and isinstance(exp.object, UnProperty)
        }

    def _split_shadowed_properties(self, pkg: "UnPackage") -> None:
        """Obfuscate local property/struct-field names shadowed by a reference.

        A locally-defined property whose name happens to match an import, a Core
        symbol, an external function, or a stringified name shares that name's
        table entry, so the external-reference exclusions preserve it and the
        local definition never gets obfuscated (e.g. ``UserFlags``,
        ``PackageName``, ``bFire``/``bAltFire`` struct fields, ``Guid``/
        ``PlayerID``). This gives each such local definition its own fresh name
        entry — leaving the shared entry for the genuine external reference — so
        the gate obfuscates the copy while the reference stays intact. Bytecode
        variable access follows the property export (by object ref), and the
        class's default-property tags are re-linked to the copy below.

        Guards: a name used as a string literal (:meth:`_reflection_property_names`)
        is skipped (runtime property-reflection reference). Struct fields ARE split
        even when their struct type carries compiled member defaults — the nested
        default data (StructProperty / array-of-struct) is re-pointed to the split
        copy by :meth:`_repoint_default_data`. Runs after the exclusion phases so
        statuses are known.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        leak_statuses = {
            ObfuscationStatus.IMPORT_REFERENCE,
            ObfuscationStatus.CORE_REFERENCE,
            ObfuscationStatus.EXTERNAL_FUNCTION_NAME,
            ObfuscationStatus.STRINGIFIED_NAME,
            # A plain local var can also shadow a genuine config/localized property
            # of the same name (e.g. MyKickHandler.WhatToDo vs the config
            # WhatToDo in the settings). Split the NON-config definition only (the
            # config one is filtered out below by its own flags).
            ObfuscationStatus.CONFIG_LOCALIZED_PROPERTY,
            # A local definition can also shadow a property name that a stored
            # object's tagged data addresses by string — the `Format` parameter
            # vs the ConsoleBox texture's `Engine.Bitmap.Format` tag. Split the
            # definition so it is still obfuscated; the tag keeps the entry.
            ObfuscationStatus.OBJECT_DATA_TAG,
        }
        protected_ids = self._reflection_protected
        cfg_mask = int(
            UnPropertyFlags.Config
            | UnPropertyFlags.Localized
            | UnPropertyFlags.GlobalConfig
        )

        # (id(struct_export), field_name) -> fresh copy, for nested-default re-link.
        field_copies: Dict[Tuple[int, str], "UnName"] = {}
        split_any = False
        for export in pkg.exports:
            obj = export.object
            if not isinstance(obj, UnProperty):
                continue
            parent = export.group_item
            parent_obj = parent.object if isinstance(parent, UnExport) else None
            # Only properties owned by a local code container (class/state/
            # function/struct); UnClass/UnState/UnFunction all derive UnStruct.
            if not isinstance(parent_obj, UnStruct):
                continue
            entry = export.object_name
            status = self.name_status.get(id(entry))
            if status is None or status[1] not in leak_statuses:
                continue
            if id(export) in protected_ids:
                continue
            # A config/localized/globalconfig property is addressed by string in
            # the INI/`.int`, so it can never be split off and renamed —
            # regardless of which rule claimed the shared entry. Its own status
            # is only CONFIG_LOCALIZED_PROPERTY when nothing earlier claimed it:
            # ``LogFileName`` is *also* an ``Engine.FileLog`` import, so it lands
            # on IMPORT_REFERENCE, and testing the status alone used to split
            # (and rename) both of a package's genuine ``config LogFileName``
            # declarations, breaking the INI key.
            if obj.property_flags & cfg_mask:
                continue
            copy = UnName(entry.name, entry.flags)
            pkg.names.append(copy)
            export.object_name = copy
            if type(parent_obj) is UnStruct:
                field_copies[(id(parent), entry.name)] = copy
            split_any = True

        if not split_any:
            return

        # Re-link every top-level default/instance tag to the current entry of
        # the local property it names (the fresh copy for split properties, the
        # original entry otherwise), and re-point the names embedded in nested
        # struct / array-of-struct default data. Tags naming an inherited-from-
        # import (non-local) property resolve to nothing and are left untouched.
        prop_cache: Dict[int, Dict[str, "UnExport"]] = {}

        def resolve(class_item, name):
            cur = class_item
            while isinstance(cur, UnExport):
                props = prop_cache.get(id(cur))
                if props is None:
                    props = self._own_properties(pkg, cur)
                    prop_cache[id(cur)] = props
                if name in props:
                    return props[name]
                cur = cur.super_item
            return None

        for export in pkg.exports:
            obj = export.object
            tag_groups: List[Tuple[Any, Any]] = []
            if isinstance(obj, UnClass):
                tag_groups.append((export, obj.default_properties))
            inst_tags = getattr(obj, "tagged_properties", None)
            if inst_tags:
                tag_groups.append((export.class_item, inst_tags))
            for owning_class, tags in tag_groups:
                if not isinstance(owning_class, UnExport):
                    continue
                for tag in tags:
                    if tag.tag_name is None:
                        continue
                    # Re-point nested struct/array default data FIRST (it locates
                    # the array's inner type by the tag's *current* name).
                    if tag.property_data:
                        self._repoint_tag_data(pkg, tag, field_copies)
                    prop = resolve(owning_class, tag.tag_name.name)
                    if prop is not None:
                        tag.tag_name = prop.object_name
        pkg._invalidate_caches()

    def _repoint_tag_data(self, pkg, tag, field_copies):
        """Re-point member names inside a StructProperty / array-of-struct tag's
        raw default data to the split-copy entries (top-level entry point).

        Args:
            pkg (UnPackage): The owning package.
            tag (UnPropertyTag): The default/instance tag to re-point in place.
            field_copies (Dict[Tuple[int, str], UnName]): (struct-export id, field
                name) -> fresh copy entry for every split struct field.
        """
        if tag.struct_name_entry is not None:
            inner = self._find_class_export(pkg, tag.struct_name_entry.name)
            if inner is not None:
                tag.property_data = self._repoint_struct_data(
                    pkg, tag.property_data, inner, field_copies
                )
        elif tag.type == int(UnNameMap.ArrayProperty):
            _, struct_ref = _find_array_inner_info(tag.tag_name.name, pkg)
            if struct_ref:
                inner = self._find_class_export(pkg, struct_ref.split(".")[-1])
                if inner is not None:
                    tag.property_data = self._repoint_array_data(
                        pkg, tag.property_data, inner, field_copies
                    )

    def _repoint_struct_data(self, pkg, data, struct_export, field_copies):
        """Return ``data`` (one struct blob: tagged members + None) with member
        name indices re-pointed to the split copies of ``struct_export``'s fields.
        Native-serialize structs (Vector/Rotator/Color) are positional and carry
        no names, so they are returned unchanged."""
        if not data or _is_native_serialize_struct(struct_export.object_name.name):
            return data
        buf = io.BytesIO(data)
        out = io.BytesIO()
        self._repoint_stream(pkg, buf, out, struct_export, field_copies)
        out.write(buf.read())
        return out.getvalue()

    def _repoint_array_data(self, pkg, data, inner_struct, field_copies):
        """Return array-of-struct ``data`` (count + N element blobs) with each
        element's member names re-pointed. Native-serialize inner structs are
        returned unchanged."""
        if not data or _is_native_serialize_struct(inner_struct.object_name.name):
            return data
        buf = io.BytesIO(data)
        out = io.BytesIO()
        count = read_index(buf)
        write_index(out, count)
        for _ in range(count):
            self._repoint_stream(pkg, buf, out, inner_struct, field_copies)
        out.write(buf.read())
        return out.getvalue()

    def _repoint_stream(self, pkg, buf, out, struct_export, field_copies):
        """Copy one tagged-member stream (until the None terminator) from ``buf``
        to ``out``, re-pointing member names to split copies and recursing into
        nested struct / array-of-struct member data."""
        while True:
            idx = read_index(buf)
            entry = pkg.names[idx] if 0 <= idx < len(pkg.names) else None
            if entry is None or entry.name == "None":
                write_index(out, idx)
                return
            tag = UnPropertyTag()
            tag.name_index = idx
            tag.tag_name = entry
            tag.parse(buf, package=pkg)
            if tag.property_data:
                if tag.struct_name_entry is not None:
                    inner = self._find_class_export(pkg, tag.struct_name_entry.name)
                    if inner is not None:
                        tag.property_data = self._repoint_struct_data(
                            pkg, tag.property_data, inner, field_copies
                        )
                elif tag.type == int(UnNameMap.ArrayProperty):
                    _, struct_ref = _find_array_inner_info(
                        entry.name,
                        pkg,
                        parent_struct_name=struct_export.object_name.name,
                    )
                    if struct_ref:
                        inner = self._find_class_export(pkg, struct_ref.split(".")[-1])
                        if inner is not None:
                            tag.property_data = self._repoint_array_data(
                                pkg, tag.property_data, inner, field_copies
                            )
            copy = field_copies.get((id(struct_export), entry.name))
            write_index(out, pkg.name_index(copy if copy is not None else entry))
            tag.serialize(out, package=pkg)

    def _config_typed_enum_ids(self, pkg: "UnPackage") -> Set[int]:
        """Return id() of the type-name entry of every enum that is the type of a
        config/localized/globalconfig property. Such an enum's value names are
        stored by name in the INI/`.int` (e.g. ``WhatToDo=Kick``), so neither the
        enum nor its values may be obfuscated."""
        cfg = int(
            UnPropertyFlags.Config
            | UnPropertyFlags.Localized
            | UnPropertyFlags.GlobalConfig
        )
        ids: Set[int] = set()
        for export in pkg.exports:
            obj = export.object
            if (
                isinstance(obj, UnByteProperty)
                and (obj.property_flags & cfg)
                and isinstance(obj.enum_item, UnExport)
            ):
                ids.add(id(obj.enum_item.object_name))
        return ids

    def _string_const_values(self, pkg: "UnPackage") -> Set[str]:
        """Return every string-constant value in the bytecode (used to protect
        enum values whose name might be string-compared, e.g. a stringified enum
        value tested against a literal)."""
        vals: Set[str] = set()
        for export in pkg.exports:
            parser = getattr(export.object, "token_parser", None)
            if parser is None:
                continue
            for token in parser.iter_all_tokens():
                if isinstance(token, UnTokenStringConst):
                    vals.add(token.value)
        return vals

    def _obfuscate_enum_values(self, pkg: "UnPackage") -> Set[int]:
        """Give each obfuscatable enum's value names fresh entries to obfuscate.

        An enum's value names are only observable through ``GetEnum`` (a runtime
        name lookup into the enum's ``names`` list) — bytecode references values
        by byte index. So when the enum's TYPE is being obfuscated (not preserved
        by any rule) and it isn't the type of a config/localized property, its
        value names can be obfuscated too. Each value is unshared onto a fresh
        entry (leaving any coincidental other user of the string intact) and
        returned for the gate to rewrite. (An enum used in a config property is
        excluded because the INI/`.int` stores the value by name.)

        Args:
            pkg (UnPackage): The package being obfuscated.

        Returns:
            Set[int]: id() of the fresh value-name entries to rewrite.
        """
        excluded = self._config_typed_enum_ids(pkg)
        extra_ids: Set[int] = set()
        for export in pkg.exports:
            obj = export.object
            if not isinstance(obj, UnEnum):
                continue
            # Enum type preserved (external ref / config / etc.) -> keep values.
            if id(export.object_name) in self.name_status:
                continue
            if id(export.object_name) in excluded:
                continue
            for i, value in enumerate(obj.names):
                if value.name.lower() == "none":
                    continue
                copy = UnName(value.name, value.flags)
                pkg.names.append(copy)
                obj.names[i] = copy
                extra_ids.add(id(copy))
        return extra_ids

    # ------------------------------------------------------------------ #
    #  Shared (multi-package) obfuscation helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dependency_order(packages: List["UnPackage"]) -> List["UnPackage"]:
        """Order ``packages`` so a package follows every sibling it imports.

        Dependencies are read straight off each package's import table (the
        root package of every import), restricted to the family being
        obfuscated.  A dependency must be rewritten first so its symbols are
        already in the shared map when its dependents are processed.

        Args:
            packages (List[UnPackage]): The family to order.

        Returns:
            List[UnPackage]: The same packages, dependencies first.

        Raises:
            SharedObfuscationError: On a duplicate package name or an import
                cycle within the family.
        """
        by_name: Dict[str, "UnPackage"] = {}
        for pkg in packages:
            name = Obfuscator._simple_name(pkg)
            if name in by_name:
                raise SharedObfuscationError(
                    f"Package {name!r} was listed twice; each package in a "
                    f"shared obfuscation run must be distinct."
                )
            by_name[name] = pkg

        deps: Dict[str, Set[str]] = {}
        for name, pkg in by_name.items():
            roots = {Obfuscator._root_package_of_import(imp) for imp in pkg.imports}
            deps[name] = {r for r in roots if r in by_name and r != name}

        ordered: List["UnPackage"] = []
        done: Set[str] = set()
        remaining = list(by_name)
        while remaining:
            ready = [n for n in remaining if deps[n] <= done]
            if not ready:
                raise SharedObfuscationError(
                    "Import cycle between packages being obfuscated together: "
                    + ", ".join(sorted(remaining))
                )
            for name in ready:
                ordered.append(by_name[name])
                done.add(name)
            remaining = [n for n in remaining if n not in done]
        return ordered

    def _shareable_strings(self, packages: List["UnPackage"]) -> Set[str]:
        """Return the name strings that must rename identically family-wide.

        A symbol only needs a shared token when a sibling can resolve it *by
        string*, which happens two ways:

        * the sibling imports it — the linker matches the import's dotted
          path against the exporting package's names; and
        * the sibling names it in bytecode or property data — the
          name-locked strings of :meth:`_collect_name_locked_strings`
          (virtual/global/delegate calls, ``NameConst``\\ s, labels, tag and
          struct names, config sections, enum values, …).

        Everything else — function locals and parameters above all — is
        reached only through an export index, so each package is free to
        give it its own token.  Keeping the shared set tight matters: a
        shared token is by definition reused, and reuse costs obfuscation
        strength.

        Must be called **before** any package in the family is rewritten.

        Args:
            packages (List[UnPackage]): The family being obfuscated.

        Returns:
            Set[str]: Name strings whose token is shared family-wide.
        """
        family = {self._simple_name(pkg) for pkg in packages}
        strings: Set[str] = set()
        for pkg in packages:
            strings |= self._collect_name_locked_strings(pkg)
            for imp in pkg.imports:
                if self._root_package_of_import(imp) not in family:
                    continue
                strings.update(self._import_relative_path(imp).split("."))
        return strings

    def _sibling_import_paths(
        self, pkg: "UnPackage", packages: List["UnPackage"]
    ) -> Set[str]:
        """Return the paths in ``pkg`` that its siblings import.

        Args:
            pkg (UnPackage): The package whose exported surface is wanted.
            packages (List[UnPackage]): The whole family (``pkg`` included).

        Returns:
            Set[str]: Dotted paths relative to ``pkg`` (e.g.
                ``MyBaseClass.bEnabled``) that a sibling imports.
        """
        own_name = self._simple_name(pkg)
        paths: Set[str] = set()
        for other in packages:
            if other is pkg:
                continue
            for imp in other.imports:
                if self._root_package_of_import(imp) == own_name:
                    paths.add(self._import_relative_path(imp))
        return paths

    def obfuscate_packages(
        self,
        packages: List["UnPackage"],
        obf_type: "ObfuscationType",
        exceptions: List[str],
        always: Optional[List[str]] = None,
        keep_public: Optional[List[str]] = None,
        retain_privacy: bool = False,
    ) -> List["UnPackage"]:
        """Obfuscate a whole package family in one shared pass.

        The family is rewritten dependency-first, and every symbol a sibling
        can reach by string is renamed to the *same* token in every package
        that mentions it, so all the cross-package links survive.  Because
        those links are kept by renaming both ends, an import into a sibling
        no longer forces its name to be preserved — a package family
        obfuscates as thoroughly as a single package would.

        A single :class:`Obfuscator` drives the whole run, so one symbol
        allocator (and therefore one uniqueness guarantee) covers every
        package, and :meth:`write_name_map` afterwards emits one map for the
        whole family.

        Args:
            packages (List[UnPackage]): The packages to obfuscate together.
                Each must have been loaded with its objects parsed and with
                its siblings wired into ``imported_packages`` — see
                :meth:`PackageLoader.load_packages_with_dependencies`.
            obf_type (ObfuscationType): Whether to use simple or harder
                symbol generation.
            exceptions (List[str]): Names to leave untouched, family-wide.
            always (Optional[List[str]]): Names to ALWAYS obfuscate.
                Defaults to None.
            keep_public (Optional[List[str]]): Field exports whose ``Public``
                object flag must be kept. Defaults to None.
            retain_privacy (bool): Force ``Private`` on every obfuscated
                export instead of merely dropping ``Public``. Defaults to
                False.

        Returns:
            List[UnPackage]: The packages in the order they were processed
                (dependencies first).

        Raises:
            SharedObfuscationError: On a duplicate name, an import cycle, or
                a symbol a sibling needs that cannot be renamed/hidden as
                requested.
        """
        ordered = self._dependency_order(packages)
        family = [self._simple_name(pkg) for pkg in ordered]
        if len(ordered) > 1:
            print(f"Shared obfuscation order: {' -> '.join(family)}")

        shareable = self._shareable_strings(ordered)
        # Names one member's stored object data addresses in another member's
        # class: every package has to leave those readable (see
        # :meth:`_sibling_data_tag_names`).
        sibling_tags = self._sibling_data_tag_names(ordered)
        if sibling_tags:
            print(
                "Preserving for a sibling's stored data: "
                + ", ".join(sorted(sibling_tags))
            )
        required = {
            self._simple_name(pkg): self._sibling_import_paths(pkg, ordered)
            for pkg in ordered
        }
        shared_symbols: Dict[str, str] = {}

        for pkg in ordered:
            name = self._simple_name(pkg)
            self.obfuscate(
                pkg,
                obf_type,
                exceptions,
                always=always,
                keep_public=keep_public,
                co_obfuscated={n for n in family if n != name},
                shared_symbols=shared_symbols,
                shareable_names=shareable,
                required_exports=required[name],
                sibling_data_tags=sibling_tags,
                retain_privacy=retain_privacy,
            )
        return ordered

    def _shareable_name_ids(
        self, pkg: "UnPackage", shareable_names: Set[str]
    ) -> Set[int]:
        """Return id() of the name entries eligible for the shared map.

        Only entries whose string is in ``shareable_names`` qualify, and only
        when the entry belongs to something a sibling could name: an export
        that is not nested inside a function (a function's locals and
        parameters are unreachable from another package) or an enum value.

        Args:
            pkg (UnPackage): The package being obfuscated.
            shareable_names (Set[str]): Strings whose token is shared.

        Returns:
            Set[int]: id() of the name entries whose token to publish.
        """
        ids: Set[int] = set()
        for export in pkg.exports:
            if export.object_name.name not in shareable_names:
                continue
            owner: Optional["UnPackageItem"] = export.group_item
            nested_in_function = False
            while isinstance(owner, UnExport):
                if owner.class_name_string == "Core.Function":
                    nested_in_function = True
                    break
                owner = owner.group_item
            if not nested_in_function:
                ids.add(id(export.object_name))
        for export in pkg.exports:
            obj = export.object
            if isinstance(obj, UnEnum):
                for value in obj.names:
                    if value.name in shareable_names:
                        ids.add(id(value))
        return ids

    def _co_import_name_ids(self, pkg: "UnPackage") -> Set[int]:
        """Return id() of the name entries a co-obfuscated link depends on.

        These are the object names of every import that points into a
        sibling being obfuscated in the same run.  The linker matches them
        by string against the sibling's (rewritten) export names, so they
        must follow the sibling's rename exactly — this is the one case that
        outranks a preservation rule.

        Args:
            pkg (UnPackage): The package being obfuscated.

        Returns:
            Set[int]: id() of the name entries that must track a sibling.
        """
        return {
            id(imp.object_name)
            for imp in pkg.imports
            if self._is_co_obfuscated_import(imp)
        }

    def _resolve_required_exports(
        self, pkg: "UnPackage", required_exports: Set[str]
    ) -> Set[int]:
        """Resolve sibling-imported paths to id() of the matching exports.

        Must run before the rewrite pass, while the export names still read
        as the importing sibling spells them.

        Args:
            pkg (UnPackage): The package being obfuscated.
            required_exports (Set[str]): Dotted paths relative to ``pkg``.

        Returns:
            Set[int]: id() of the exports a sibling imports.
        """
        if not required_exports:
            return set()
        return {
            id(export)
            for export in pkg.exports
            if export.object_name_string in required_exports
        }

    def _is_visibility_exempt(
        self, export: "UnExport", keep_set: Set[str], required_ids: Set[int]
    ) -> bool:
        """Whether ``export`` must keep the visibility it was compiled with.

        Three kinds are left exactly as they are: one the caller named in
        ``keep_public`` (a package outside this run has to resolve it), one a
        co-obfuscated sibling imports (the linker rejects a non-Public import
        target), and one this package reaches by string through
        ``Set/GetPropertyText`` — the engine requires ``RF_Public`` on the
        property before it will resolve one by name at all, so dropping it
        makes the lookup fail and the assignment silently not happen.

        Args:
            export (UnExport): The export to test.
            keep_set (Set[str]): Lower-cased keep-public names.
            required_ids (Set[int]): id() of exports a sibling imports.

        Returns:
            bool: True when the export's visibility must not be touched.
        """
        if keep_set and self._matches_keep_public(export, keep_set):
            return True
        if id(export) in self._reflection_protected:
            return True
        return id(export) in required_ids

    def _harden_declarations(
        self, pkg: "UnPackage", keep_set: Set[str], required_ids: Set[int]
    ) -> None:
        """Mark every obfuscated declaration ``private`` (and vars ``editconst``).

        Dropping ``Public`` hides an export from the *linker*, but says nothing
        about how its **declaration** reads: a decompile of the obfuscated
        package still shows plain ``var`` declarations and public functions.
        Setting the declaration-level modifiers as well is both more honest —
        nothing here is meant to be reachable — and one more thing an attacker
        has to undo, since ucc rejects a recovered ``local editconst``/``var
        private`` outright (``Disallow = ~0`` for a local: *"Specified type
        modifiers not allowed here"*).

        The encodings are the ones ``ucc`` itself emits (see the specifier
        parsing in the script compiler):

        * a variable — member, struct field or function local alike — gets
          ``CPF_EditConst`` plus ``RF_Final`` and no ``RF_Public``;
        * a function gets ``FUNC_Private`` and no ``FUNC_Public``.

        None of it changes runtime behaviour: those flags are read by the
        compiler and the editor, never by the VM (property access is by offset,
        function dispatch by index). Parameters and return values are therefore
        skipped — a modifier is not legal on them, and an ``out`` parameter has
        to stay writable.

        Args:
            pkg (UnPackage): The package being obfuscated.
            keep_set (Set[str]): Lower-cased keep-public names.
            required_ids (Set[int]): id() of exports a sibling imports.
        """
        for export in pkg.exports:
            if self._is_visibility_exempt(export, keep_set, required_ids):
                continue
            obj = export.object
            if isinstance(obj, UnProperty):
                if obj.property_flags & int(UnPropertyFlags.Parm):
                    continue
                obj.property_flags |= int(UnPropertyFlags.EditConst)
                export.flags &= ~UnObjectFlags.Public
                export.flags |= UnObjectFlags.Private
            elif isinstance(obj, UnFunction):
                obj.function_flags &= ~UnFunctionFlags.Public
                obj.function_flags |= UnFunctionFlags.Private

    def _apply_export_privacy(
        self,
        pkg: "UnPackage",
        keep_set: Set[str],
        required_ids: Set[int],
        retain_privacy: bool,
    ) -> None:
        """Hide the obfuscated exports, honouring the sibling links.

        Every export loses its ``Public`` flag so the obfuscated package
        re-exports nothing under its new names.  Exports listed in
        ``keep_public`` are left exactly as they are (a package outside this run
        still has to find them), and so is any property this package itself
        reaches by string: ``Set/GetPropertyText`` require ``RF_Public`` on the
        property and silently do nothing without it, so a hidden
        reflection target reads back as ``None``.

        Anything a sibling imports is exempt, whatever its kind.  UE2's linker
        verifies every import against the exporting package and refuses one
        whose target export is not ``Public``::

            Can't import private object Function MyMod.MyBaseClass.<fn>
                (when loading Class'MyModPlus.MyDerivedClass')

        That check is on the *object flag*, and it applies to classes,
        properties and functions alike — it is not the same thing as the script
        ``private`` modifier, which only gates property access.  So hiding a
        sibling-imported export of any kind severs the link.

        With ``retain_privacy`` the hidden exports are additionally marked
        ``Private``.  A *property* a sibling imports is then an error, because
        the caller has to decide what to do (stop importing it, add it to
        ``keep_public``, or drop ``retain_privacy``); the other kinds simply
        stay ``Public``, since they cannot be hidden at all.

        Args:
            pkg (UnPackage): The package being obfuscated.
            keep_set (Set[str]): Lower-cased keep-public names.
            required_ids (Set[int]): id() of exports a sibling imports.
            retain_privacy (bool): Whether to enforce ``Private``.

        Raises:
            SharedObfuscationError: When ``retain_privacy`` is set and a
                sibling imports a property of ``pkg``.
        """
        blocked: List[str] = []
        for export in pkg.exports:
            # Not _is_visibility_exempt(): a sibling-imported export is handled
            # below, since under retain_privacy a *property* among them is an
            # error rather than something to skip quietly.
            if keep_set and self._matches_keep_public(export, keep_set):
                continue
            if id(export) in self._reflection_protected:
                # A Set/GetPropertyText target: the engine looks it up by name
                # AND requires RF_Public, so hiding it breaks the lookup.
                continue
            if id(export) in required_ids:
                if retain_privacy and isinstance(export.object, UnProperty):
                    blocked.append(
                        self.original_names.get(
                            id(export.object_name), export.object_name.name
                        )
                    )
                # Keep it Public: the linker verifies the sibling's import
                # against this export and rejects a non-Public target.
                continue
            export.flags &= ~UnObjectFlags.Public
            if retain_privacy:
                export.flags |= UnObjectFlags.Private
        if blocked:
            raise SharedObfuscationError(
                f"--retain-privacy cannot be honoured for "
                f"{self._simple_name(pkg)}: another package obfuscated in "
                f"the same run imports the propert"
                f"{'y' if len(blocked) == 1 else 'ies'} "
                f"{', '.join(sorted(blocked))}, and Unreal enforces the "
                f"Private flag on property access.  Access them through a "
                f"function instead, list them in --keep-public, or drop "
                f"--retain-privacy.  (Either way the export keeps its Public "
                f"flag — the linker refuses to import a non-Public object.)"
            )

    # ------------------------------------------------------------------ #
    #  Main entry point
    # ------------------------------------------------------------------ #

    def obfuscate(
        self,
        pkg: "UnPackage",
        obf_type: "ObfuscationType",
        exceptions: List[str],
        always: Optional[List[str]] = None,
        keep_public: Optional[List[str]] = None,
        *,
        co_obfuscated: Optional[Set[str]] = None,
        shared_symbols: Optional[Dict[str, str]] = None,
        shareable_names: Optional[Set[str]] = None,
        required_exports: Optional[Set[str]] = None,
        sibling_data_tags: Optional[Set[str]] = None,
        retain_privacy: bool = False,
    ) -> Dict[int, Tuple["UnName", "ObfuscationStatus"]]:
        """Obfuscate the names of ``pkg`` in place.

        The decision for every name is recorded in :attr:`name_status`
        and returned. A name is preserved unless its final status is
        :attr:`ObfuscationStatus.OBFUSCATED`.

        The keyword-only arguments describe a shared, multi-package run and
        are normally supplied by :meth:`obfuscate_packages` rather than by
        hand.

        Args:
            pkg (UnPackage): The package to obfuscate.
            obf_type (ObfuscationType): Whether to use simple or harder
                symbol generation.
            exceptions (List[str]): Names the caller wants left untouched.
            always (Optional[List[str]]): Names to ALWAYS obfuscate, overriding
                every preservation rule. Each matching local code definition is
                rewritten with a **simple** glyph token even in harder mode (so
                a forced symbol can never inject control bytes into an INI/config
                key). Defaults to None.
            keep_public (Optional[List[str]]): Field exports whose ``Public``
                object flag must be KEPT (not stripped), so runtime reflection
                (``Set/GetPropertyText``) can still reach them. Each entry is a
                bare field name (``MySettings``) or a qualified
                ``Class.Field`` (``MyBaseClass.MySettings``), matched
                case-insensitively against pre-obfuscation names. Defaults to
                None.
            co_obfuscated (Optional[Set[str]]): Simple names of the sibling
                packages being obfuscated in the same run. References into
                those packages are renamed rather than preserved. Defaults to
                None.
            shared_symbols (Optional[Dict[str, str]]): Mutable
                ``original name -> token`` map shared by the whole family, so
                one symbol renames identically everywhere. Defaults to None
                (each name gets a fresh token).
            shareable_names (Optional[Set[str]]): The strings that may cross a
                package boundary; only these publish their token into
                ``shared_symbols``. Defaults to None (nothing is published).
            required_exports (Optional[Set[str]]): Dotted paths (relative to
                ``pkg``) that a sibling imports, used to decide export
                visibility. Defaults to None.
            sibling_data_tags (Optional[Set[str]]): Names a sibling's stored
                object data addresses, which must stay readable here — see
                :meth:`_sibling_data_tag_names`. Defaults to None.
            retain_privacy (bool): Mark every hidden export ``Private``
                instead of merely dropping ``Public``. Defaults to False.

        Returns:
            Dict[int, Tuple[UnName, ObfuscationStatus]]: A mapping from
                ``id(UnName)`` to the surviving name and its disposition.

        Raises:
            RuntimeError: If ``pkg`` itself is the Core package.
            SharedObfuscationError: If a symbol a sibling links against
                cannot be renamed consistently, or cannot be made private
                while ``retain_privacy`` is set.
        """
        simple_name = self._simple_name(pkg)
        if simple_name.lower() == "core":
            raise RuntimeError(
                "Refusing to obfuscate the Core package — "
                "it is the source of every preserved built-in name."
            )

        # Drop the generation history before we start rewriting names.
        # Generation bookkeeping locks the older portion of the name
        # table against mutation, which would otherwise prevent the
        # obfuscator from touching any pre-existing name.  The drop also
        # deduplicates the name table and regenerates the package GUID,
        # both of which are desirable for the obfuscated output.
        pkg.drop_generations()

        self.name_status = {}
        self.original_names = {}
        self._co_obfuscated = set(co_obfuscated or ())
        self._co_obfuscated.discard(simple_name)

        # Which of this package's exports a sibling links against. Resolved
        # now, while the export names still read the way the sibling spells
        # them.
        required_ids = self._resolve_required_exports(pkg, set(required_exports or ()))

        # Property exports looked up by string via Set/GetPropertyText — these
        # must keep their name. Computed before un-sharing so the un-share pass
        # can keep them on the shared (preserved) entry.
        self._reflection_protected = self._reflection_protected_property_ids(pkg)

        # Un-share names so each variable definition owns a unique entry and
        # can be obfuscated to its own distinct symbol (see
        # :meth:`_unshare_object_names`).
        self._unshare_object_names(pkg)

        # Always preserve "None" (the tagged-property terminator).
        for name_entry in pkg.names:
            if name_entry.name.lower() == "none":
                self._mark(name_entry, ObfuscationStatus.NONE_NAME)

        # A name a sibling's stored object data addresses must stay readable
        # here, whatever else it is: the referencing package cannot follow a
        # rename it has no import for. First, so this wins the mark race and the
        # definition is never split off and obfuscated.
        self._exclude_sibling_data_tags(pkg, sibling_data_tags)
        # User exceptions first, so an EXCEPTION disposition wins the (first-
        # write-wins) _mark race over CORE/IMPORT/etc. — otherwise a shadowed
        # exception name (e.g. VersionInfo.PackageName, which also matches a Core
        # name) would keep a leak status and be split-and-obfuscated below.
        self._exclude_exceptions(pkg, exceptions)
        self._exclude_core_references(pkg)
        self._exclude_imports(pkg)
        # Before the superclass rules: a commandlet's chain often also trips the
        # config-inheritance rule, and "commandlet class" is the useful reason
        # to report (both preserve the name either way).
        self._exclude_commandlet_classes(pkg)
        self._exclude_external_superclasses(pkg)
        self._exclude_function_overrides(pkg)
        self._exclude_external_function_names(pkg)
        self._exclude_config_localized(pkg)
        self._exclude_native(pkg)
        self._exclude_native_class_variables(pkg)
        self._exclude_functions_invoked_by_name(pkg)
        self._exclude_state_names(pkg)
        self._exclude_stringified_names(pkg)
        self._exclude_stringified_enums(pkg)
        # Names that a stored object's tagged data addresses by string (a
        # texture/sound/shader/component's engine property, an inherited
        # property named in defaultproperties).
        self._exclude_object_data_tags(pkg)

        # Decouple local property/struct-field definitions that merely share a
        # name with an external/observable reference, so they get obfuscated
        # while the genuine reference keeps its name.
        self._split_shadowed_properties(pkg)

        # Obfuscate the value names of enums that are themselves obfuscatable
        # (not config-typed, not otherwise preserved). These entries are not
        # export object-names, so collect them for the rewrite gate below.
        enum_value_ids = self._obfuscate_enum_values(pkg)

        # Only a name that names a locally-defined *code* export may be
        # rewritten.  Every other name is a *reference* — to an import, an
        # inherited property, an engine state/function, or a content object
        # named by value in property/token data — and is resolved by string
        # outside this package, so it must be preserved.  References to
        # *local* symbols stay correct automatically: they resolve through
        # the name table by index, so renaming the entry updates the
        # definition and every reference to it in lockstep.
        #
        # The class of the export must be a code class (Class / Struct /
        # Function / State / property / … — see ``CODE_CLASS_NAMES``).
        # Content-instance exports (Texture, Sound, Emitter, GUI widgets,
        # …) are left alone: their names are not code symbols and may be
        # referenced by string.
        local_definition_ids = {
            id(exp.object_name)
            for exp in pkg.exports
            if exp.class_name_string.split(".")[-1] in CODE_CLASS_NAMES
        }

        # Build the rewrite list: a name is rewritten if it names a local export
        # and no exclusion rule preserved it, OR it is force-listed in ``always``
        # (which overrides every preservation), OR a sibling being obfuscated in
        # the same run already renamed it (so this package must follow). Any
        # remaining non-local name is recorded as an external reference.
        always_set = set(always or [])
        keep_set = {k.lower() for k in (keep_public or [])}
        shared = shared_symbols if shared_symbols is not None else None
        shareable = set(shareable_names or ())
        publish_ids = (
            self._shareable_name_ids(pkg, shareable)
            if shared is not None and shareable
            else set()
        )
        # Names an import into a sibling resolves by string: they must track
        # that sibling's rename even if a local rule would preserve them.
        linked_ids = self._co_import_name_ids(pkg) if shared is not None else set()

        forced_ids: Set[int] = set()
        names_to_rewrite: List["UnName"] = []
        for name_entry in pkg.names:
            is_local = (
                id(name_entry) in local_definition_ids
                or id(name_entry) in enum_value_ids
            )
            forced = is_local and name_entry.name in always_set
            already_marked = id(name_entry) in self.name_status
            # A sibling either renamed this symbol (we must match its token) or
            # preserved it (we must preserve it too).
            linked = id(name_entry) in linked_ids
            sibling_token = shared.get(name_entry.name) if shared is not None else None
            if linked and sibling_token is None:
                # The sibling kept this name, so the link still reads as it did.
                self._mark(name_entry, ObfuscationStatus.IMPORT_REFERENCE)
                continue
            if already_marked and linked:
                status = self.name_status[id(name_entry)][1]
                raise SharedObfuscationError(
                    f"{simple_name} imports {name_entry.name!r} from a package "
                    f"obfuscated in the same run, which renamed it, but "
                    f"{name_entry.name!r} must be preserved here "
                    f"({status.value}).  Add it to the exceptions list so both "
                    f"packages keep it readable."
                )
            if already_marked and not forced:
                continue
            if not already_marked and not is_local and sibling_token is None:
                self._mark(name_entry, ObfuscationStatus.EXTERNAL_REFERENCE)
                continue
            if forced:
                forced_ids.add(id(name_entry))
            names_to_rewrite.append(name_entry)

        # Snapshot every entry's original text (originals and split copies
        # alike) before the rewrite pass so callers can recover it.
        self.original_names = {id(n): n.name for n in pkg.names}

        # Identify the name entries of keep-public exports (matched on their
        # pre-obfuscation names). A kept-public field retains its Public flag,
        # so it stays exported and resolvable by name; if such a symbol is
        # nonetheless obfuscated, its new name must be a clean glyph token
        # (never a harder-mode control-byte hash) so the exported name can't
        # be corrupted — the same guarantee ``always``-forced symbols get.
        keep_public_name_ids: Set[int] = set()
        if keep_set:
            for export in pkg.exports:
                if self._matches_keep_public(export, keep_set):
                    keep_public_name_ids.add(id(export.object_name))

        if names_to_rewrite:
            if not self._seeded:
                # The harder-mode generator carries a running index; start it at
                # a randomised offset so the first emitted value isn't a fixed,
                # recognisable one. Seeded once per obfuscator: a shared run
                # walks a single index across every package, which is what keeps
                # those tokens unique. The glyph generator needs no seed — it
                # draws every character and dedupes what it issued (gen_simple).
                self._hash_index = self._gen.randint(350000, 500000)
                self._seeded = True

            for name_entry in names_to_rewrite:
                original = name_entry.name
                h = shared.get(original) if shared is not None else None
                if h is None:
                    # Forced (``always``) and kept-public symbols always use a
                    # simple glyph token — even in harder mode — so they can
                    # never introduce control bytes into an INI/config key or an
                    # exported (reflection-resolvable) symbol name.
                    if (
                        (obf_type & ObfuscationType.SIMPLE)
                        or id(name_entry) in forced_ids
                        or id(name_entry) in keep_public_name_ids
                    ):
                        h = self.gen_simple()
                    else:
                        h = self.gen_hash()
                    if shared is not None and id(name_entry) in publish_ids:
                        shared[original] = h
                name_entry.name = h
                self.name_status[id(name_entry)] = (
                    name_entry,
                    ObfuscationStatus.OBFUSCATED,
                )
                self._map_entries[h] = original

        # Hide the exports so the obfuscated package re-exports nothing under
        # its new names, keeping whatever a sibling has to link against.
        self._apply_export_privacy(pkg, keep_set, required_ids, retain_privacy)
        # ...and mark the declarations themselves private/editconst, so the
        # obfuscated package does not read as public API either.
        self._harden_declarations(pkg, keep_set, required_ids)

        self._print_disposition()

        # Deduplicate the name table again: un-shared copies that ended up
        # preserved (rather than rewritten) now share a string once more, and
        # rewritten copies are unique.  Merging leaves each string used once.
        pkg.deduplicate_names()

        # Reconcile the status map with the surviving name entries (dedup may
        # have dropped merged-away duplicates).
        surviving = {id(n) for n in pkg.names}
        self.name_status = {
            key: value for key, value in self.name_status.items() if key in surviving
        }

        return self.name_status

    def _print_disposition(self) -> None:
        """Log the outcome for each original name exactly once.

        Preserved names print with the reason they were kept; rewritten
        names print with every symbol they were hashed to (a single
        original name may map to several symbols once its definitions
        have been un-shared). A name that is both preserved and rewritten
        (a split name with mixed dispositions) is shown under its symbols.
        """
        hashed: Dict[str, List[str]] = {}
        excluded: Dict[str, "ObfuscationStatus"] = {}
        for key, original in self.original_names.items():
            entry, status = self.name_status[key]
            if status is ObfuscationStatus.OBFUSCATED:
                hashed.setdefault(original, []).append(entry.name)
            else:
                excluded.setdefault(original, status)

        for original, status in excluded.items():
            if original in hashed:
                continue
            print(f"Excluding: {original}  ({status.value})")
        for original, symbols in hashed.items():
            joined = ", ".join(repr(s) for s in symbols)
            print(f"Hashing:   {original} -> {joined}")

    # ------------------------------------------------------------------ #
    #  Misc post-processing helpers (unchanged)
    # ------------------------------------------------------------------ #

    def strip_source(self, pkg: "UnPackage") -> None:
        """Replace the script source of every text buffer with a stub.

        Args:
            pkg (UnPackage): The package whose text buffers are stripped.
        """
        for export in pkg.exports:
            if isinstance(export.object, UnTextBuffer):
                export.object.script_text = "// Source has been stripped"

    def _matches_keep_public(self, export: "UnExport", keep_set: Set[str]) -> bool:
        """Whether ``export`` is named in the keep-public set.

        Matches on the *pre-obfuscation* names (via :attr:`original_names`), so
        a field whose name was rewritten is still recognised. An export matches
        by its bare field name (``field``) or its qualified ``class.field``
        (using the owning class from the ``group_item`` chain), compared
        case-insensitively.

        Args:
            export (UnExport): The export to test.
            keep_set (Set[str]): Lower-cased names/qualified names to keep public.

        Returns:
            bool: True if the export should retain its Public flag.
        """
        field = self.original_names.get(id(export.object_name))
        if field is None:
            field = export.object_name.name
        candidates = {field.lower()}
        owner = export.group_item
        if owner is not None and owner.object_name is not None:
            cls = self.original_names.get(id(owner.object_name), owner.object_name.name)
            candidates.add(f"{cls}.{field}".lower())
        return bool(candidates & keep_set)

    @staticmethod
    def _is_plain_token(token: str) -> bool:
        """Return whether ``token`` can sit verbatim on a map line.

        A plain token is non-empty printable ASCII with no whitespace and no
        ``#``/``=`` (which would collide with the ``token = name`` / comment
        syntax).  Simple-mode glyph symbols qualify; harder-mode hashes
        (newlines + control bytes) do not and must be base64-encoded.

        Args:
            token (str): The obfuscated symbol.

        Returns:
            bool: True if the token is safe to write unencoded.
        """
        if not token:
            return False
        return all(0x20 < ord(ch) < 0x7F and ch not in "#=" for ch in token)

    def write_name_map(self, path: str) -> int:
        """Write an ``obfuscated -> original`` name map for :meth:`deobfuscate`.

        Emits one ``<ObfuscatedToken> = <OriginalName>`` line per rewritten name,
        which :func:`ut2004packageutil.deobfuscator.parse_map_file` can consume to
        rename the package back to its original symbols.  Simple-mode symbols are
        written verbatim; if any token is not printable (harder-mode hashes carry
        newlines and control bytes), the whole token column is base64-encoded and
        the header is flagged with :data:`TOKEN_ENCODING_MARKER`.

        Must be called after :meth:`obfuscate` (or
        :meth:`obfuscate_packages`), whose bookkeeping this reads.  The map
        accumulates across every package of a shared run, so one file
        deobfuscates the whole family — the tokens are shared, so a symbol
        present in several packages needs only the one line.

        Args:
            path (str): Destination map file path.

        Returns:
            int: The number of mapped (rewritten) names written.
        """
        entries: List[Tuple[str, str]] = [
            (token, original) for token, original in self._map_entries.items()
        ]
        entries.sort(key=lambda item: (item[1].lower(), item[1]))

        use_base64 = any(not self._is_plain_token(token) for token, _ in entries)

        with open(path, "w", encoding="latin-1") as handle:
            handle.write("# UT2004PackageUtil obfuscation map\n")
            handle.write("# Format: <ObfuscatedToken> = <OriginalName>\n")
            if use_base64:
                handle.write(f"# {TOKEN_ENCODING_MARKER}\n")
            for token, original in entries:
                column = encode_map_token(token) if use_base64 else token
                handle.write(f"{column} = {original}\n")

        return len(entries)
