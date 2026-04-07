"""Package obfuscation utilities."""

import os
import random
from enum import Enum, IntFlag
from typing import Dict, List, Optional, Set, Tuple

from ut2004packageutil.deobfuscator import TOKEN_ENCODING_MARKER, encode_map_token
from ut2004packageutil.package.flags import (
    UnFunctionFlags,
    UnObjectFlags,
    UnPropertyFlags,
    UnStructFlags,
)
from ut2004packageutil.package.object import (
    CODE_CLASS_NAMES,
    UnClass,
    UnEnum,
    UnField,
    UnFunction,
    UnProperty,
    UnStruct,
    UnTextBuffer,
)
from ut2004packageutil.package.package import (
    UnExport,
    UnImport,
    UnName,
    UnPackage,
    UnPackageItem,
)
from ut2004packageutil.package.token import (
    UnCastType,
    UnTokenDelegateFunction,
    UnTokenDelegateProperty,
    UnTokenGlobalFunction,
    UnTokenLabelTable,
    UnTokenNameConst,
    UnTokenPrimitiveCast,
    UnTokenVirtualFunction,
)


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
    CORE_REFERENCE = "Core reference"
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
    STATE_NAME = "state name"
    STRINGIFIED_NAME = "stringified name"


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
        self._gen = random.Random()
        self.name_status: Dict[int, Tuple["UnName", "ObfuscationStatus"]] = {}
        # id(UnName) -> the name's string *before* the rewrite pass, captured
        # after names are un-shared.  Lets callers recover the original text
        # of each surviving entry (including freshly split copies).
        self.original_names: Dict[int, str] = {}

    # ------------------------------------------------------------------ #
    #  Hash generators
    # ------------------------------------------------------------------ #

    def gen_hash(self) -> str:
        """Generate a unique, hard-to-read symbol string.

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
            # Keep the first definition on the original entry; give every
            # other definition a fresh copy so each is used exactly once.
            for export in exports[1:]:
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

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for imp in pkg.imports:
            self._mark(imp.object_name, ObfuscationStatus.IMPORT_REFERENCE)
            self._mark(imp.class_name, ObfuscationStatus.IMPORT_REFERENCE)
            self._mark(imp.class_package_name, ObfuscationStatus.IMPORT_REFERENCE)
            # Walk the group chain (an import may live inside another import)
            gi: Optional["UnPackageItem"] = imp.group_item
            while gi is not None:
                self._mark(gi.object_name, ObfuscationStatus.IMPORT_REFERENCE)
                gi = gi.group_item

    def _exclude_external_superclasses(self, pkg: "UnPackage") -> None:
        """Preserve class/struct names whose super lives outside (phase 4).

        An ``UnExport`` of class ``Core.Class`` or ``Core.Struct`` whose
        immediate super is an :class:`UnImport` keeps its name verbatim
        because subclass behaviour relies on the original identifier.

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        for export in pkg.exports:
            if export.class_name_string not in ("Core.Class", "Core.Struct"):
                continue
            super_item = export.super_item
            while super_item is not None:
                if isinstance(super_item, UnImport):
                    self._mark(
                        export.object_name,
                        ObfuscationStatus.EXTERNAL_SUPERCLASS,
                    )
                    break
                # An UnExport — keep walking the chain inside this package.
                if isinstance(super_item, UnExport):
                    super_item = super_item.super_item
                else:
                    break

    def _exclude_function_overrides(self, pkg: "UnPackage") -> None:
        """Preserve functions that override an external function (phase 5).

        For every ``Core.Function`` export we walk the parent class's
        super chain. Once the chain leaves this package (an
        :class:`UnImport`), we cross into the dependency package and check
        whether the external class (and its own ancestors) declares a
        function with the same name. If so, the local function name must
        remain stable.

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

            while cur_super is not None:
                if isinstance(cur_super, UnImport):
                    ext_pkg_name = self._root_package_of_import(cur_super)
                    ext_pkg = pkg.imported_packages.get(ext_pkg_name)
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
                    if func_name in self._functions_of_class(ext_pkg, ext_class):
                        self._mark(
                            export.object_name,
                            ObfuscationStatus.EXTERNAL_FUNCTION_OVERRIDE,
                        )
                        break
                    cur_pkg = ext_pkg
                    cur_super = ext_class.super_item
                    crossed_external = True
                elif isinstance(cur_super, UnExport):
                    if crossed_external:
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

        Args:
            pkg (UnPackage): The package being obfuscated.
        """
        external_func_names: Set[str] = set()
        for dep_pkg in pkg.imported_packages.values():
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

    # ------------------------------------------------------------------ #
    #  Main entry point
    # ------------------------------------------------------------------ #

    def obfuscate(
        self,
        pkg: "UnPackage",
        obf_type: "ObfuscationType",
        exceptions: List[str],
    ) -> Dict[int, Tuple["UnName", "ObfuscationStatus"]]:
        """Obfuscate the names of ``pkg`` in place.

        The decision for every name is recorded in :attr:`name_status`
        and returned. A name is preserved unless its final status is
        :attr:`ObfuscationStatus.OBFUSCATED`.

        Args:
            pkg (UnPackage): The package to obfuscate.
            obf_type (ObfuscationType): Whether to use simple or harder
                symbol generation.
            exceptions (List[str]): Names the caller wants left untouched.

        Returns:
            Dict[int, Tuple[UnName, ObfuscationStatus]]: A mapping from
                ``id(UnName)`` to the surviving name and its disposition.

        Raises:
            RuntimeError: If ``pkg`` itself is the Core package.
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

        # Un-share names so each variable definition owns a unique entry and
        # can be obfuscated to its own distinct symbol (see
        # :meth:`_unshare_object_names`).
        self._unshare_object_names(pkg)

        # Always preserve "None" (the tagged-property terminator).
        for name_entry in pkg.names:
            if name_entry.name.lower() == "none":
                self._mark(name_entry, ObfuscationStatus.NONE_NAME)

        self._exclude_core_references(pkg)
        self._exclude_exceptions(pkg, exceptions)
        self._exclude_imports(pkg)
        self._exclude_external_superclasses(pkg)
        self._exclude_function_overrides(pkg)
        self._exclude_external_function_names(pkg)
        self._exclude_config_localized(pkg)
        self._exclude_native(pkg)
        self._exclude_native_class_variables(pkg)
        self._exclude_functions_invoked_by_name(pkg)
        self._exclude_state_names(pkg)
        self._exclude_stringified_names(pkg)

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

        # Build the rewrite list: a name is rewritten only if it names a
        # local export and no exclusion rule already preserved it.  Any
        # remaining non-local name is recorded as an external reference.
        names_to_rewrite: List["UnName"] = []
        for name_entry in pkg.names:
            if id(name_entry) in self.name_status:
                continue
            if id(name_entry) not in local_definition_ids:
                self._mark(name_entry, ObfuscationStatus.EXTERNAL_REFERENCE)
                continue
            names_to_rewrite.append(name_entry)

        # Snapshot every entry's original text (originals and split copies
        # alike) before the rewrite pass so callers can recover it.
        self.original_names = {id(n): n.name for n in pkg.names}

        if names_to_rewrite:
            if obf_type & ObfuscationType.SIMPLE:
                # Start at a randomised offset (100000 + 1..1000) so the
                # first emitted index isn't a fixed, recognisable value.
                self._hash_index = 100000 + self._gen.randint(1, 1000)
            else:
                self._hash_index = self._gen.randint(350000, 500000)

            for name_entry in names_to_rewrite:
                if obf_type & ObfuscationType.SIMPLE:
                    h = f"O{self._hash_index}"
                    # Advance the index by a random step so the emitted
                    # numbers are non-consecutive (harder to correlate)
                    # while staying monotonic and unique.
                    self._hash_index += self._gen.randint(1, 1000)
                else:
                    h = self.gen_hash()
                name_entry.name = h
                self.name_status[id(name_entry)] = (
                    name_entry,
                    ObfuscationStatus.OBFUSCATED,
                )

        # Strip the Public bit on every export so the obfuscated package
        # doesn't accidentally re-export newly-renamed symbols.
        for export in pkg.exports:
            export.flags &= ~UnObjectFlags.Public

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
                export.object.script_text = "//No source for you."

    @staticmethod
    def _is_plain_token(token: str) -> bool:
        """Return whether ``token`` can sit verbatim on a map line.

        A plain token is non-empty printable ASCII with no whitespace and no
        ``#``/``=`` (which would collide with the ``token = name`` / comment
        syntax).  Simple-mode ``O<index>`` symbols qualify; harder-mode hashes
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

        Must be called after :meth:`obfuscate`, whose bookkeeping this reads.

        Args:
            path (str): Destination map file path.

        Returns:
            int: The number of mapped (rewritten) names written.
        """
        entries: List[Tuple[str, str]] = []
        for key, (entry, status) in self.name_status.items():
            if status is not ObfuscationStatus.OBFUSCATED:
                continue
            original = self.original_names.get(key, entry.name)
            entries.append((entry.name, original))
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
