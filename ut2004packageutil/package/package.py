"""Unreal package container, package items, and import/export types."""

from abc import ABC, abstractmethod
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    TypeVar,
    Union,
    overload,
)

from ut2004packageutil.package.flags import (
    UnGuid,
    UnObjectFlags,
    UnPackageFlags,
)

# ===================================================================== #
#  Reference helpers (compact index ↔ package item)
# ===================================================================== #


def resolve_item(pkg: "UnPackage", index: int) -> Optional["UnPackageItem"]:
    """Resolve a compact reference index to a package item.

    Args:
        pkg ("UnPackage"): The package that owns the imports and exports.
        index (int): Compact reference index (0 is none, positive is an
            export, negative is an import).

    Returns:
        Optional["UnPackageItem"]: The referenced item, or None when index
        is 0.
    """
    if index == 0:
        return None
    if index > 0:
        return pkg.exports[index - 1]
    return pkg.imports[-index - 1]


def link_item(pkg: "UnPackage", item: Optional["UnPackageItem"]) -> int:
    """Convert a package item back to a compact reference index.

    Args:
        pkg ("UnPackage"): The package that owns the imports and exports.
        item (Optional["UnPackageItem"]): The item to reference, or None.

    Returns:
        int: The compact reference index for the item.
    """
    return pkg.item_index(item)


# ===================================================================== #
#  Object factory registry
# ===================================================================== #
#
# ``UnExport.create_object()`` needs to instantiate the concrete
# :class:`UnObject` subclass for an export's class name, but the
# implementations live in :mod:`ut2004packageutil.package.object` — which
# itself imports from this module.  To avoid the circular import the
# object factory is registered here at import time of ``object.py``.

_object_factory: Optional[Callable[["UnExport"], Optional[Any]]] = None


def register_object_factory(
    factory: Callable[["UnExport"], Optional[Any]],
) -> None:
    """Register the function used by :meth:`UnExport.create_object`.

    The factory takes an :class:`UnExport` and returns the appropriate
    :class:`UnObject` subclass instance (or None if the class is unknown).
    Called once from :mod:`ut2004packageutil.package.object`.

    Args:
        factory (Callable[["UnExport"], Optional[Any]]): Factory that maps
            an export to its concrete object instance or None.
    """
    global _object_factory
    _object_factory = factory


def create_object_for_export(export: "UnExport") -> Optional[Any]:
    """Invoke the registered object factory for the export.

    Args:
        export ("UnExport"): The export to instantiate an object for.

    Returns:
        Optional[Any]: The created object, or None when no factory has been
        registered yet (e.g. during bootstrap before object.py is imported).
    """
    if _object_factory is None:
        return None
    return _object_factory(export)


# ===================================================================== #
#  Simple types
# ===================================================================== #


class UnName:
    """Represents a single entry in the package name table."""

    def __init__(self, name: str, flags: "UnObjectFlags") -> None:
        """Initialize a name table entry.

        Args:
            name (str): The name string.
            flags ("UnObjectFlags"): Object flags associated with the name.
        """
        self.name = name
        self.flags = flags

    def __repr__(self) -> str:
        """Return the debug representation of the name entry.

        Returns:
            str: A string of the form ``UnName(name, flags)``.
        """
        return f"UnName({self.name!r}, {self.flags!r})"


class UnGeneration:
    """Represents a generation record in the package header."""

    def __init__(self, export_count: int, name_count: int) -> None:
        """Initialize a generation record.

        Args:
            export_count (int): Number of exports in this generation.
            name_count (int): Number of names in this generation.
        """
        self.export_count = export_count
        self.name_count = name_count

    def __repr__(self) -> str:
        """Return the debug representation of the generation record.

        Returns:
            str: A string of the form ``UnGeneration(exports=..., names=...)``.
        """
        return f"UnGeneration(exports={self.export_count}, names={self.name_count})"


# ===================================================================== #
#  UnGenerationList
# ===================================================================== #

T = TypeVar("T")


class UnGenerationList(Generic[T]):
    """A list wrapper that tracks generation boundaries.

    Items at indices below ``_last_generation_count`` belong to a previous
    generation and are protected from mutation when ``_keep_generation`` is
    True.  When ``_last_generation_count`` is -1 all operations are
    unrestricted regardless of ``_keep_generation``.
    """

    def __init__(self) -> None:
        """Initialize an empty generation list with no generation boundary."""
        self._list: List[T] = []
        self._last_generation_count: int = -1
        self._keep_generation: bool = True

    # -- read access (always allowed) ---------------------------------- #

    def __len__(self) -> int:
        """Return the number of items in the list.

        Returns:
            int: The item count.
        """
        return len(self._list)

    @overload
    def __getitem__(self, index: int) -> T: ...
    @overload
    def __getitem__(self, index: slice) -> List[T]: ...
    def __getitem__(self, index: "Union[int, slice]") -> "Union[T, List[T]]":
        """Return the item(s) at the given index or slice.

        Args:
            index ("Union[int, slice]"): Index or slice to retrieve.

        Returns:
            "Union[T, List[T]]": A single item for an int index or a list of
            items for a slice.
        """
        return self._list[index]

    def __iter__(self) -> "Iterator[T]":
        """Return an iterator over the list items.

        Returns:
            "Iterator[T]": An iterator over the contained items.
        """
        return iter(self._list)

    def __contains__(self, item: object) -> bool:
        """Return whether the item is present in the list.

        Args:
            item (object): The item to test for membership.

        Returns:
            bool: True if the item is present, False otherwise.
        """
        return item in self._list

    def index(self, item: T, *args: int) -> int:
        """Return the index of the first occurrence of the item.

        Args:
            item (T): The item to locate.
            *args (int): Optional start and stop bounds forwarded to
                ``list.index``.

        Returns:
            int: The index of the first matching item.
        """
        return self._list.index(item, *args)

    def __bool__(self) -> bool:
        """Return whether the list is non-empty.

        Returns:
            bool: True if the list contains at least one item.
        """
        return bool(self._list)

    # -- helpers ------------------------------------------------------- #

    def _check_mutable(self, index: int) -> None:
        """Raise if the index is in the protected generation range.

        Args:
            index (int): The absolute index being mutated.

        Raises:
            RuntimeError: When the index belongs to a previous generation
                and generation protection is enabled.
        """
        if (
            self._keep_generation
            and self._last_generation_count >= 0
            and index < self._last_generation_count
        ):
            raise RuntimeError(
                f"Cannot mutate item at index {index}: it belongs to a "
                f"previous generation (boundary={self._last_generation_count})."
            )

    # -- mutators ------------------------------------------------------ #

    def append(self, item: T) -> None:
        """Append an item to the end of the list.

        Args:
            item (T): The item to append.
        """
        self._list.append(item)

    def insert(self, index: int, item: T) -> None:
        """Insert an item at the given index.

        Args:
            index (int): The position at which to insert.
            item (T): The item to insert.

        Raises:
            RuntimeError: When the index belongs to a protected generation.
        """
        self._check_mutable(index)
        self._list.insert(index, item)

    def pop(self, index: int = -1) -> T:
        """Remove and return the item at the given index.

        Args:
            index (int): The index to pop. Defaults to -1 (the last item).

        Returns:
            T: The removed item.

        Raises:
            RuntimeError: When the index belongs to a protected generation.
        """
        real_idx = index if index >= 0 else len(self._list) + index
        self._check_mutable(real_idx)
        return self._list.pop(index)

    def remove(self, item: T) -> None:
        """Remove the first occurrence of the item.

        Args:
            item (T): The item to remove.

        Raises:
            RuntimeError: When the item belongs to a protected generation.
        """
        idx = self._list.index(item)
        self._check_mutable(idx)
        self._list.remove(item)

    def __setitem__(self, index: int, value: T) -> None:
        """Replace the item at the given index.

        Args:
            index (int): The index to overwrite.
            value (T): The new value.

        Raises:
            RuntimeError: When the index belongs to a protected generation.
        """
        self._check_mutable(index)
        self._list[index] = value

    def __delitem__(self, index: int) -> None:
        """Delete the item at the given index.

        Args:
            index (int): The index to delete.

        Raises:
            RuntimeError: When the index belongs to a protected generation.
        """
        real_idx = index if index >= 0 else len(self._list) + index
        self._check_mutable(real_idx)
        del self._list[index]

    def clear(self) -> None:
        """Remove all items from the list.

        Raises:
            RuntimeError: When previous-generation items are protected.
        """
        if self._keep_generation and self._last_generation_count > 0:
            raise RuntimeError(
                "Cannot clear list: previous generation items are protected."
            )
        self._list.clear()

    def extend(self, items: "List[T]") -> None:
        """Append multiple items to the end of the list.

        Args:
            items ("List[T]"): The items to append.
        """
        self._list.extend(items)

    # -- generation management ----------------------------------------- #

    @property
    def last_generation_count(self) -> int:
        """Return the generation boundary index.

        Returns:
            int: The boundary index, or -1 when unrestricted.
        """
        return self._last_generation_count

    @last_generation_count.setter
    def last_generation_count(self, value: int) -> None:
        """Set the generation boundary index.

        Args:
            value (int): The new boundary index (-1 for unrestricted).
        """
        self._last_generation_count = value

    @property
    def keep_generation(self) -> bool:
        """Return whether generation protection is enabled.

        Returns:
            bool: True when protected items cannot be mutated.
        """
        return self._keep_generation

    @keep_generation.setter
    def keep_generation(self, value: bool) -> None:
        """Set whether generation protection is enabled.

        Args:
            value (bool): True to protect previous-generation items.
        """
        self._keep_generation = value

    def reset_generation(self) -> None:
        """Remove the generation boundary, allowing full mutation."""
        self._last_generation_count = -1

    def __repr__(self) -> str:
        """Return the debug representation of the generation list.

        Returns:
            str: A string describing the length and generation boundary.
        """
        return (
            f"UnGenerationList(len={len(self._list)}, "
            f"gen_boundary={self._last_generation_count})"
        )


# ===================================================================== #
#  UnPackageItem
# ===================================================================== #


class UnPackageItem(ABC):
    """Abstract base class for imports and exports in an Unreal package."""

    def __init__(
        self,
        package: "UnPackage",
        object_name: "UnName",
        group_index: int = 0,
        group_item: Optional["UnPackageItem"] = None,
    ) -> None:
        """Initialize a package item.

        Args:
            package ("UnPackage"): The owning package.
            object_name ("UnName"): The item's object name entry.
            group_index (int): Compact reference index of the group parent.
                Defaults to 0.
            group_item (Optional["UnPackageItem"]): Resolved group parent
                item. Defaults to None.
        """
        self.children: List["UnExport"] = []
        self.package = package
        self.object_name = object_name
        self.group_index = group_index
        self.group_item = group_item
        self.group_data: str = ""

    @property
    def object_name_string(self) -> str:
        """Return the fully qualified dotted object name.

        Returns:
            str: The object name prefixed by its group chain (e.g.
            ``Actor.Role``).
        """
        if self.group_item is None:
            return self.object_name.name
        return self.group_item.object_name_string + "." + self.object_name.name

    @property
    def class_name_string(self) -> str:
        """Return the item's class name string.

        Returns:
            str: The class name; empty for the base implementation.
        """
        return ""

    @property
    @abstractmethod
    def object(self) -> Optional[Any]:
        """Return the parsed object for this item, or None.

        ``UnExport`` returns its parsed ``UnObject`` (if resolved).
        ``UnImport`` looks up the corresponding export in the dependent
        package and returns its object.

        Returns:
            Optional[Any]: The parsed object, or None when unavailable.
        """
        ...

    def link(self) -> None:
        """Reindex references for this item."""
        self.group_index = self.package.item_index(self.group_item)

    def resolve(self) -> None:
        """Resolve integer references to actual item objects."""
        self.group_item = resolve_item(self.package, self.group_index)

    def clear_resolved(self) -> None:
        """Unlink all resolved references."""
        self.group_item = None
        self.children.clear()

    def drop_generations(self) -> None:
        """Clean up this item for generation-free export.

        The base implementation is a no-op.
        """
        pass


# ===================================================================== #
#  UnImport
# ===================================================================== #


class UnImport(UnPackageItem):
    """Represents an import entry in an Unreal package."""

    def __init__(
        self,
        package: "UnPackage",
        object_name: "UnName",
        group_index: int = 0,
        group_item: Optional["UnPackageItem"] = None,
        *,
        class_package_name: Optional["UnName"] = None,
        class_name: Optional["UnName"] = None,
    ) -> None:
        """Initialize an import entry.

        Args:
            package ("UnPackage"): The owning package.
            object_name ("UnName"): The import's object name entry.
            group_index (int): Compact reference index of the group parent.
                Defaults to 0.
            group_item (Optional["UnPackageItem"]): Resolved group parent
                item. Defaults to None.
            class_package_name (Optional["UnName"]): Name of the package that
                defines the import's class. Defaults to None.
            class_name (Optional["UnName"]): Name of the import's class.
                Defaults to None.
        """
        super().__init__(
            package,
            object_name,
            group_index=group_index,
            group_item=group_item,
        )
        self.class_package_name = class_package_name
        self.class_name = class_name

    @property
    def class_name_string(self) -> str:
        """Return the import's dotted ``package.class`` name.

        Returns:
            str: The class name formatted as ``package.class``.
        """
        pkg = self.class_package_name.name if self.class_package_name else ""
        cls = self.class_name.name if self.class_name else ""
        return f"{pkg}.{cls}"

    @property
    def object(self) -> Optional[Any]:
        """Look up the corresponding export in the dependent package.

        The source package is the root of this import's group chain (a
        top-level ``Package`` import), **not** ``class_package_name`` — the
        latter names the package that defines this import's *class* (e.g.
        ``Core`` for a ``ByteProperty``), which is rarely where the object
        lives.  The export is matched by its path relative to that package
        (``Actor.Role``), falling back to a bare object-name match.

        Returns:
            Optional[Any]: The resolved object from the dependent package,
            or None when no match is found.
        """
        root = self._root_package_name()
        full = self.object_name_string
        rel = full[len(root) + 1 :] if root and full.startswith(root + ".") else full

        candidate_names = []
        if root:
            candidate_names.append(root)
        if self.class_package_name is not None:
            candidate_names.append(self.class_package_name.name)

        for pkg_name in candidate_names:
            imported_pkg = self.package.imported_packages.get(pkg_name)
            if imported_pkg is None:
                continue
            for exp in imported_pkg.exports:
                if exp.object_name_string == rel:
                    return exp.object
            for exp in imported_pkg.exports:
                if exp.object_name.name == self.object_name.name:
                    return exp.object
        return None

    def _root_package_name(self) -> str:
        """Return the name of the top-level package this import descends from.

        Returns:
            str: The root package name, or an empty string when it cannot be
            determined.
        """
        cur: Optional["UnPackageItem"] = self
        while isinstance(cur, UnImport) and cur.group_item is not None:
            cur = cur.group_item
        return cur.object_name.name if isinstance(cur, UnImport) else ""

    def __str__(self) -> str:
        """Return a human-readable description of the import.

        Returns:
            str: The object name with its ``(package.class)`` suffix.
        """
        pkg = self.class_package_name.name if self.class_package_name else ""
        cls = self.class_name.name if self.class_name else ""
        return f"{self.object_name_string} ({pkg}.{cls})"

    def __repr__(self) -> str:
        """Return the debug representation of the import.

        Returns:
            str: A string of the form ``UnImport(object_name_string)``.
        """
        return f"UnImport({self.object_name_string!r})"


# ===================================================================== #
#  UnExport
# ===================================================================== #


class UnExport(UnPackageItem):
    """Represents an export entry in an Unreal package."""

    def __init__(
        self,
        package: "UnPackage",
        object_name: "UnName",
        group_index: int = 0,
        group_item: Optional["UnPackageItem"] = None,
        *,
        class_index: int = 0,
        super_index: int = 0,
        flags: "UnObjectFlags" = UnObjectFlags(0),
        export_offset: int = -1,
        export_size: int = -1,
        class_item: Optional["UnPackageItem"] = None,
        super_item: Optional["UnPackageItem"] = None,
        export_data: Optional[bytes] = None,
    ) -> None:
        """Initialize an export entry.

        Args:
            package ("UnPackage"): The owning package.
            object_name ("UnName"): The export's object name entry.
            group_index (int): Compact reference index of the group parent.
                Defaults to 0.
            group_item (Optional["UnPackageItem"]): Resolved group parent
                item. Defaults to None.
            class_index (int): Compact reference index of the class.
                Defaults to 0.
            super_index (int): Compact reference index of the super item.
                Defaults to 0.
            flags ("UnObjectFlags"): Object flags for the export. Defaults to
                UnObjectFlags(0).
            export_offset (int): Byte offset of the export data. Defaults
                to -1.
            export_size (int): Byte size of the export data. Defaults to -1.
            class_item (Optional["UnPackageItem"]): Resolved class item.
                Defaults to None.
            super_item (Optional["UnPackageItem"]): Resolved super item.
                Defaults to None.
            export_data (Optional[bytes]): Raw export payload. Defaults to
                None.
        """
        super().__init__(
            package,
            object_name,
            group_index=group_index,
            group_item=group_item,
        )
        self.class_index = class_index
        self.class_item = class_item
        self.class_data: str = ""

        self.super_index = super_index
        self.super_item = super_item
        self.super_data: str = ""

        self.export_offset = export_offset
        self.export_size = export_size
        self.export_data = export_data
        # Byte offset the export data was read from in the source file.  Kept
        # so writers can fix up absolute file offsets embedded in an unparsed
        # blob (e.g. a Texture's TLazyArray mip SkipOffset) when the object
        # moves to a new position in the output file.
        self.original_data_offset: int = -1
        self.flags = flags
        self._object: Optional["UnObject"] = None

    @property
    def object(self) -> Optional[Any]:
        """Return the parsed object for this export, or None.

        Returns:
            Optional[Any]: The parsed object, or None when not yet created.
        """
        return self._object

    @object.setter
    def object(self, value: Optional[Any]) -> None:
        """Set the parsed object and register it with its super item.

        Args:
            value (Optional[Any]): The parsed object to assign.
        """
        self._object = value

        if self.super_item is not None:
            self.super_item.children.append(self)

    def __str__(self) -> str:
        """Return a human-readable description of the export.

        Returns:
            str: The object name and class name (``name: class``).
        """
        return f"{self.object_name_string}: {self.class_name_string}"

    def __repr__(self) -> str:
        """Return the debug representation of the export.

        Returns:
            str: A string of the form ``UnExport(object_name_string)``.
        """
        return f"UnExport({self.object_name_string!r})"

    @property
    def class_name_string(self) -> str:
        """Return the export's class name string.

        Returns:
            str: The class item's object name, the super item's class name,
            or an empty string when neither is set.
        """
        if self.class_item is not None:
            return self.class_item.object_name_string
        if self.super_item is not None:
            return self.super_item.class_name_string
        return ""

    def link(self) -> None:
        """Reindex group, class, and super references for this export."""
        super().link()
        self.class_index = self.package.item_index(self.class_item)
        self.super_index = self.package.item_index(self.super_item)

    def resolve(self) -> None:
        """Resolve group, class, and super integer references to items."""
        super().resolve()
        self.class_item = resolve_item(self.package, self.class_index)
        self.super_item = resolve_item(self.package, self.super_index)
        if self.super_item is not None:
            self.super_item.children.append(self)

    def clear_resolved(self) -> None:
        """Unlink resolved group, class, and super item references."""
        super().clear_resolved()
        self.class_item = None
        self.super_item = None

    def drop_generations(self) -> None:
        """Clean up this export for generation-free serialisation."""
        if self._object is not None:
            self._object.drop_generations()

    def create_object(self) -> None:
        """Instantiate the appropriate object type based on class name.

        Dispatches to the factory registered by
        :mod:`ut2004packageutil.package.object` at import time.  If that
        module hasn't been imported yet, ``self._object`` is left as None.
        """
        self._object = create_object_for_export(self)


# ===================================================================== #
#  UnPackage
# ===================================================================== #


class UnPackage:
    """Root container for an Unreal package (.u file)."""

    MAGIC_HEADER = 0x9E2A83C1

    def __init__(self) -> None:
        """Initialize an empty package with default header fields."""
        self.generations: List["UnGeneration"] = []
        self.names: "UnGenerationList[UnName]" = UnGenerationList()
        self.imports: List["UnImport"] = []
        self.exports: "UnGenerationList[UnExport]" = UnGenerationList()
        self.imported_packages: Dict[str, "UnPackage"] = {}
        self.loader: Optional[Any] = None  # PackageLoader (avoids circular import)

        self.name: str = ""
        self.version: int = 0
        self.licensee_version: int = 0
        self.flags: "UnPackageFlags" = UnPackageFlags(0)
        self.guid: "UnGuid" = UnGuid()
        self.objects_loaded: bool = False

        # A placeholder package stands in for a content-only dependency
        # (see :meth:`PackageLoader.load_with_dependencies`) that could not
        # be located on disk.  It carries no names/imports/exports, so any
        # import that targets it resolves its ``object`` to ``None`` — it is
        # never dereferenced and the import keeps its original reference.
        self.is_placeholder: bool = False

        # Lookup caches (built lazily, invalidated on mutation)
        self._name_str_to_indices: Optional[Dict[str, List[int]]] = None
        self._name_id_to_index: Optional[Dict[int, int]] = None
        self._import_ons_to_indices: Optional[Dict[str, List[int]]] = None
        self._export_ons_to_indices: Optional[Dict[str, List[int]]] = None
        self._item_id_to_index: Optional[Dict[int, int]] = None

    # ------------------------------------------------------------------ #
    #  Lookup caches
    # ------------------------------------------------------------------ #

    def _invalidate_caches(self) -> None:
        """Invalidate all lookup caches.

        Called after structural mutations to the name table or item lists.
        """
        self._name_str_to_indices = None
        self._name_id_to_index = None
        self._import_ons_to_indices = None
        self._export_ons_to_indices = None
        self._item_id_to_index = None

    def _invalidate_name_caches(self) -> None:
        """Invalidate only name-related caches."""
        self._name_str_to_indices = None
        self._name_id_to_index = None

    def _invalidate_item_caches(self) -> None:
        """Invalidate only item-reference caches."""
        self._import_ons_to_indices = None
        self._export_ons_to_indices = None
        self._item_id_to_index = None

    def _build_name_caches(self) -> None:
        """Build name lookup caches if not already built."""
        if self._name_str_to_indices is not None:
            return
        str_to_indices: Dict[str, List[int]] = {}
        id_to_index: Dict[int, int] = {}
        for i, n in enumerate(self.names):
            str_to_indices.setdefault(n.name, []).append(i)
            id_to_index[id(n)] = i
        self._name_str_to_indices = str_to_indices
        self._name_id_to_index = id_to_index

    def _build_item_ref_caches(self) -> None:
        """Build item-reference lookup caches if not already built.

        Must only be called after items are linked (``group_item`` set),
        since ``object_name_string`` depends on the group chain.
        """
        if self._import_ons_to_indices is not None:
            return
        import_cache: Dict[str, List[int]] = {}
        export_cache: Dict[str, List[int]] = {}
        id_to_ref: Dict[int, int] = {}
        for i, imp in enumerate(self.imports):
            ons = imp.object_name_string
            import_cache.setdefault(ons, []).append(i)
            id_to_ref[id(imp)] = -(i + 1)
        for i, exp in enumerate(self.exports):
            ons = exp.object_name_string
            export_cache.setdefault(ons, []).append(i)
            id_to_ref[id(exp)] = i + 1
        self._import_ons_to_indices = import_cache
        self._export_ons_to_indices = export_cache
        self._item_id_to_index = id_to_ref

    def name_index(self, name: "UnName") -> int:
        """Return the index of the name in the name table.

        Lookup is O(1) via the identity cache, with a linear-scan fallback.

        Args:
            name ("UnName"): The name entry to locate.

        Returns:
            int: The index of the name in the name table.
        """
        self._build_name_caches()
        assert self._name_id_to_index is not None
        idx = self._name_id_to_index.get(id(name))
        if idx is not None:
            return idx
        # Fallback to linear scan (should not happen normally)
        return self.names.index(name)

    def item_index(self, item: Optional["UnPackageItem"]) -> int:
        """Convert a package item to a compact reference index.

        Lookup is O(1) via the identity cache, with a linear-scan fallback.

        Args:
            item (Optional["UnPackageItem"]): The item to reference, or None.

        Returns:
            int: The compact reference index (0 for None, positive for an
            export, negative for an import).

        Raises:
            RuntimeError: When the item is neither a known import nor export.
        """
        if item is None:
            return 0
        self._build_item_ref_caches()
        assert self._item_id_to_index is not None
        ref = self._item_id_to_index.get(id(item))
        if ref is not None:
            return ref
        # Fallback to linear scan (should not happen normally)
        if isinstance(item, UnExport):
            idx = self.exports.index(item)
            if idx != -1:
                return idx + 1
        elif isinstance(item, UnImport):
            idx = self.imports.index(item)
            if idx != -1:
                return -(idx + 1)
        raise RuntimeError("Item cannot be referenced.")

    # ------------------------------------------------------------------ #
    #  Adding entries
    # ------------------------------------------------------------------ #

    def add_generation(self, export_count: int, name_count: int) -> None:
        """Append a generation record to the package header.

        Args:
            export_count (int): Number of exports in the generation.
            name_count (int): Number of names in the generation.
        """
        self.generations.append(UnGeneration(export_count, name_count))

    def add_name(self, name: str, flags: "UnObjectFlags") -> None:
        """Append a name entry to the name table and invalidate name caches.

        Args:
            name (str): The name string.
            flags ("UnObjectFlags"): Object flags for the name.
        """
        self.names.append(UnName(name, flags))
        self._invalidate_name_caches()

    # ------------------------------------------------------------------ #
    #  Generation management
    # ------------------------------------------------------------------ #

    def reset_generations(self) -> None:
        """Remove all generations and unlock names/exports for full mutation."""
        self.generations.clear()
        self.names.reset_generation()
        self.exports.reset_generation()

    def remove_exports(self, exports: "Iterable[UnExport]") -> int:
        """Remove export entries (and their descendants) and invalidate caches.

        The removal set is transitively expanded to include every export
        grouped under a removed export (e.g. a function's parameters, locals
        and return value), otherwise their ``group_item`` pointers would
        dangle.  Generation protection is lifted first (via
        :meth:`reset_generations`) so entries sealed in a prior generation can
        be dropped.

        This performs no renumbering itself: callers must guarantee that no
        *surviving* resolved reference points at a removed export, then rely on
        :meth:`link` / :meth:`link_objects` (e.g. during ``write_package``) to
        re-derive every integer index from its item pointer.

        Args:
            exports ("Iterable[UnExport]"): The export entries to remove.

        Returns:
            int: The number of exports actually removed (including
            descendants).
        """
        to_remove = {id(e) for e in exports}
        if not to_remove:
            return 0
        # Transitively pull in descendants grouped under a removed export.
        changed = True
        while changed:
            changed = False
            for e in self.exports:
                if id(e) in to_remove:
                    continue
                group = e.group_item
                if group is not None and id(group) in to_remove:
                    to_remove.add(id(e))
                    changed = True
        self.reset_generations()
        kept = [e for e in self.exports if id(e) not in to_remove]
        removed = len(self.exports) - len(kept)
        self.exports.clear()
        self.exports.extend(kept)
        self._invalidate_caches()
        return removed

    def drop_generations(self) -> None:
        """Drop generation history, GUID, dedupe names, and clean up objects.

        Must be called after objects are resolved (``resolve_objects()``).

        Actions:
            - Deduplicates name table entries.
            - Re-links all integer references.
            - Calls ``drop_generations()`` on all imports and exports.
            - Resets generations and regenerates the GUID.

        Raises:
            RuntimeError: When objects are not loaded and resolved.
        """
        if not self.objects_loaded:
            raise RuntimeError(
                "Cannot drop generations: objects must be loaded and resolved first."
            )

        # 1. Deduplicate names
        self.deduplicate_names()

        # 2. Re-link to update integer references after name table changed
        self.link()
        self.link_objects()

        # 3. Propagate to imports and exports
        for imp in self.imports:
            imp.drop_generations()
        for export in self.exports:
            export.drop_generations()

        # 4. Reset generations and GUID
        self.generations.clear()
        self.names.reset_generation()
        self.exports.reset_generation()
        self.guid = UnGuid()

        # 5. Invalidate caches
        self._invalidate_caches()

    def deduplicate_names(self) -> None:
        """Remove duplicate name entries, keeping the first occurrence.

        After rebuilding the name table with unique entries, all ``UnName``
        pointers (in imports, exports, and parsed objects) are re-resolved
        via ``find_name(name.name)``.  Raw name indices embedded in token
        streams and tagged property data are remapped via
        ``remap_name_indices``.

        Must be called **after** ``resolve_objects()`` so that references
        are resolved to ``UnName`` objects.
        """
        # 1. Build canonical name map and detect duplicates
        seen: Dict[str, "UnName"] = {}
        has_dupes = False
        for name_entry in self.names:
            if name_entry.name in seen:
                has_dupes = True
            else:
                seen[name_entry.name] = name_entry

        if not has_dupes:
            return

        # 2. Build new name list (unique only) and old→new index map
        new_names: List["UnName"] = []
        added: Dict[str, int] = {}  # name_string → new_index
        for name_entry in self.names:
            if name_entry.name not in added:
                added[name_entry.name] = len(new_names)
                new_names.append(seen[name_entry.name])

        index_map: Dict[int, int] = {}
        for old_idx, name_entry in enumerate(self.names):
            new_idx = added[name_entry.name]
            if old_idx != new_idx:
                index_map[old_idx] = new_idx

        # 3. Remap raw name indices BEFORE rebuilding name table
        #    (needs old name table intact so _remap_tagged_data_block
        #     can identify "None" terminators)
        if index_map:
            from ut2004packageutil.package.object import remap_blob_export_names

            for export in self.exports:
                if export.object is not None:
                    export.object.remap_name_indices(index_map)
                elif export.export_data:
                    remap_blob_export_names(export, index_map, self)

        # 4. Replace name table
        self.names._list.clear()
        self.names._list.extend(new_names)
        self._invalidate_caches()

        # 5. Re-resolve UnName pointers on imports/exports
        for imp in self.imports:
            if imp.object_name is not None:
                imp.object_name = self.find_name(imp.object_name.name)
            if imp.class_package_name is not None:
                imp.class_package_name = self.find_name(imp.class_package_name.name)
            if imp.class_name is not None:
                imp.class_name = self.find_name(imp.class_name.name)

        for export in self.exports:
            if export.object_name is not None:
                export.object_name = self.find_name(export.object_name.name)

        # 6. Re-resolve UnName pointers inside objects
        for export in self.exports:
            if export.object is not None:
                export.object.deduplicate_names()

    def prune_unused_names(self) -> int:
        """Remove name-table entries no longer referenced, renumbering the rest.

        A name is considered *used* when it is either:

        * the target of a ``UnName`` pointer written during serialisation —
          captured by a side-effect-free dry-run serialise that records every
          :meth:`name_index` lookup (this covers import/export table names,
          tag/struct names, friendly/config/None names, enum value names,
          package imports, hide categories, … — everything the writer emits);
        * a raw name index embedded in a token stream or tagged property —
          captured by replaying :meth:`UnObject.remap_name_indices` with a
          recording map.

        The two passes together are complete with respect to the serialiser,
        so only genuinely dead names are dropped.  Must be called after
        ``resolve_objects()``.

        Returns:
            int: The number of names removed.
        """
        if not self.objects_loaded:
            return 0

        from ut2004packageutil.package.object import remap_blob_export_names

        used: "set[int]" = set()

        # (a) Embedded raw name indices: replay the name-remap traversal with a
        #     map that records each looked-up index and maps it to itself.  This
        #     covers parsed objects; unparsed content blobs (object is None,
        #     e.g. textures) carry name indices in their raw export_data too, so
        #     record those as well — and refuse to prune if any such blob cannot
        #     be safely walked (a HasStack object), to avoid dangling indices.
        class _Recorder(dict):
            def get(self, key, default=None):  # type: ignore[override]
                used.add(key)
                return key

        recorder = _Recorder()
        for export in self.exports:
            if export.object is not None:
                export.object.remap_name_indices(recorder)
            elif export.export_data:
                if not remap_blob_export_names(export, recorder, self):
                    return 0  # Unhandleable blob — do not renumber names.

        # (b) Pointer-site names: dry-run serialise, recording every
        #     name_index() lookup.  Header state is snapshotted and restored so
        #     the run leaves no trace.
        import os
        import tempfile

        from ut2004packageutil.package.io import UnPackageIO

        saved_gens = list(self.generations)
        saved_guid = self.guid
        original_name_index = self.name_index

        def _recording_name_index(name: "UnName") -> int:
            idx = original_name_index(name)
            used.add(idx)
            return idx

        self.name_index = _recording_name_index  # type: ignore[assignment]
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".u")
            os.close(fd)
            UnPackageIO().write_package(self, tmp_path)
        finally:
            self.name_index = original_name_index  # type: ignore[assignment]
            self.generations = saved_gens
            self.guid = saved_guid
            self._invalidate_caches()
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

        if len(used) >= len(self.names):
            return 0  # Every name is referenced — nothing to prune.

        # Build the old→new index map, keeping used names in original order.
        kept_old = [i for i in range(len(self.names)) if i in used]
        index_map: Dict[int, int] = {old: new for new, old in enumerate(kept_old)}
        new_names = [self.names[i] for i in kept_old]

        # Remap embedded raw indices BEFORE swapping the table (the remap needs
        # the old table intact to identify "None" terminators by string).
        for export in self.exports:
            if export.object is not None:
                export.object.remap_name_indices(index_map)
            elif export.export_data:
                remap_blob_export_names(export, index_map, self)

        # Swap in the pruned name table.
        removed = len(self.names) - len(new_names)
        self.names.reset_generation()
        self.names._list.clear()
        self.names._list.extend(new_names)
        self._invalidate_caches()

        # Re-resolve UnName pointers on imports/exports and inside objects.
        for imp in self.imports:
            if imp.object_name is not None:
                imp.object_name = self.find_name(imp.object_name.name)
            if imp.class_package_name is not None:
                imp.class_package_name = self.find_name(imp.class_package_name.name)
            if imp.class_name is not None:
                imp.class_name = self.find_name(imp.class_name.name)
        for export in self.exports:
            if export.object_name is not None:
                export.object_name = self.find_name(export.object_name.name)
        for export in self.exports:
            if export.object is not None:
                export.object.deduplicate_names()

        return removed

    def add_import(
        self,
        package_reference: int,
        class_package_name_index: int,
        class_name_index: int,
        name_index: int,
    ) -> None:
        """Create and append an import entry from name/reference indices.

        Args:
            package_reference (int): Compact reference index of the group
                parent.
            class_package_name_index (int): Name table index of the class
                package name.
            class_name_index (int): Name table index of the class name.
            name_index (int): Name table index of the object name.
        """
        self.imports.append(
            UnImport(
                self,
                self.names[name_index],
                group_index=package_reference,
                class_package_name=self.names[class_package_name_index],
                class_name=self.names[class_name_index],
            )
        )
        self._invalidate_item_caches()

    def add_export(
        self,
        object_name_index: int,
        package_name_reference: int,
        class_index: int,
        super_index: int,
        flags: "UnObjectFlags",
        export_offset: int,
        export_size: int,
    ) -> None:
        """Create and append an export entry from name/reference indices.

        Args:
            object_name_index (int): Name table index of the object name.
            package_name_reference (int): Compact reference index of the
                group parent.
            class_index (int): Compact reference index of the class.
            super_index (int): Compact reference index of the super item.
            flags ("UnObjectFlags"): Object flags for the export.
            export_offset (int): Byte offset of the export data.
            export_size (int): Byte size of the export data.
        """
        self.exports.append(
            UnExport(
                self,
                self.names[object_name_index],
                group_index=package_name_reference,
                class_index=class_index,
                super_index=super_index,
                flags=flags,
                export_offset=export_offset,
                export_size=export_size,
            )
        )
        self._invalidate_item_caches()

    # ------------------------------------------------------------------ #
    #  Look-up helpers
    # ------------------------------------------------------------------ #

    def find_name(self, name: str) -> Optional["UnName"]:
        """Find the first name table entry matching the given string.

        Args:
            name (str): The name string to look up.

        Returns:
            Optional["UnName"]: The first matching entry, or None.
        """
        self._build_name_caches()
        assert self._name_str_to_indices is not None
        indices = self._name_str_to_indices.get(name)
        if indices:
            return self.names[indices[0]]
        return None

    # ------------------------------------------------------------------ #
    #  Name index ↔ Name@N resolution
    # ------------------------------------------------------------------ #

    def resolve_name_index(self, idx: int) -> str:
        """Resolve a name table index to a string, using ``Name@N`` for dupes.

        When the name at *idx* appears more than once in the name table, the
        result uses ``Name@N`` where *N* is the 1-based occurrence count.
        When the name is unique, the plain name string is returned.

        Args:
            idx (int): The name table index to resolve.

        Returns:
            str: The resolved name string, or the stringified index when out
            of range.
        """
        if not (0 <= idx < len(self.names)):
            return str(idx)
        self._build_name_caches()
        assert self._name_str_to_indices is not None
        name_str = self.names[idx].name
        indices = self._name_str_to_indices[name_str]
        if len(indices) == 1:
            return name_str
        # Find which occurrence this is (1-based)
        for rank, i in enumerate(indices, 1):
            if i == idx:
                return f"{name_str}@{rank}"
        return str(idx)  # shouldn't reach here

    def link_name_index(self, name_ref: str) -> int:
        """Resolve a ``Name`` or ``Name@N`` string back to a name table index.

        Plain names are resolved to the first matching entry.  The ``@N``
        suffix selects the *N*-th occurrence (1-based) of the base name.

        Args:
            name_ref (str): A plain name or ``Name@N`` reference.

        Returns:
            int: The resolved name table index, or 0 when unresolvable.
        """
        if not name_ref:
            return 0
        self._build_name_caches()
        assert self._name_str_to_indices is not None
        if "@" in name_ref:
            base, suffix = name_ref.rsplit("@", 1)
            try:
                occurrence = int(suffix)
            except (ValueError, TypeError):
                pass
            else:
                indices = self._name_str_to_indices.get(base)
                if indices and 1 <= occurrence <= len(indices):
                    return indices[occurrence - 1]
        # Plain name lookup — return first match
        indices = self._name_str_to_indices.get(name_ref)
        if indices:
            return indices[0]
        try:
            return int(name_ref)
        except (ValueError, TypeError):
            return 0

    def find_name_by_ref(self, name_ref: str) -> Optional["UnName"]:
        """Find the ``UnName`` object for a ``Name`` or ``Name@N`` reference.

        Args:
            name_ref (str): A plain name or ``Name@N`` reference.

        Returns:
            Optional["UnName"]: The matching entry, or None when out of range.
        """
        idx = self.link_name_index(name_ref)
        if 0 <= idx < len(self.names):
            return self.names[idx]
        return None

    # ------------------------------------------------------------------ #
    #  Item reference ↔ prefixed name resolution
    # ------------------------------------------------------------------ #

    def resolve_item_ref(self, ref: int) -> str:
        """Resolve a compact object reference to a prefixed name string.

        Imports (ref < 0) get a ``-`` prefix, exports (ref > 0) get a ``+``
        prefix.  Zero returns the empty string.

        When multiple items share the same ``object_name_string``, a ``#N``
        suffix (1-based) is appended for disambiguation.

        Args:
            ref (int): The compact object reference index.

        Returns:
            str: The prefixed name string, or the stringified ref when out of
            range.
        """
        if ref == 0:
            return ""
        self._build_item_ref_caches()
        assert self._import_ons_to_indices is not None
        assert self._export_ons_to_indices is not None
        if ref > 0:
            idx = ref - 1
            if 0 <= idx < len(self.exports):
                ons = self.exports[idx].object_name_string
                indices = self._export_ons_to_indices.get(ons, [])
                if len(indices) > 1:
                    rank = indices.index(idx) + 1
                    return f"+{ons}#{rank}"
                return "+" + ons
        else:
            idx = -ref - 1
            if 0 <= idx < len(self.imports):
                ons = self.imports[idx].object_name_string
                indices = self._import_ons_to_indices.get(ons, [])
                if len(indices) > 1:
                    rank = indices.index(idx) + 1
                    return f"-{ons}#{rank}"
                return "-" + ons
        return str(ref)

    def link_item_ref(self, name: str) -> int:
        """Resolve a prefixed name string back to a compact object reference.

        ``-Name`` looks up imports, ``+Name`` looks up exports.
        ``#N`` suffix selects the N-th occurrence (1-based).
        Unprefixed names fall back to searching imports then exports.

        Args:
            name (str): The prefixed (or unprefixed) name string.

        Returns:
            int: The compact object reference, or 0 when unresolvable.
        """
        if not name:
            return 0
        self._build_item_ref_caches()
        assert self._import_ons_to_indices is not None
        assert self._export_ons_to_indices is not None

        # Parse prefix
        if name.startswith("-") or name.startswith("+"):
            is_import = name.startswith("-")
            bare = name[1:]
        else:
            is_import = None  # search both
            bare = name

        # Parse #N occurrence suffix
        occurrence = 0  # 0 = first match
        if "#" in bare:
            bare, suffix = bare.rsplit("#", 1)
            try:
                occurrence = int(suffix)
            except (ValueError, TypeError):
                pass

        def _lookup_imports() -> Optional[int]:
            """Return the import reference matching ``bare`` and occurrence.

            Returns:
                Optional[int]: The negative import reference, or None when no
                matching import is found.
            """
            indices = self._import_ons_to_indices.get(bare)
            if not indices:
                return None
            if occurrence == 0:
                return -(indices[0] + 1)
            if 1 <= occurrence <= len(indices):
                return -(indices[occurrence - 1] + 1)
            return None

        def _lookup_exports() -> Optional[int]:
            """Return the export reference matching ``bare`` and occurrence.

            Returns:
                Optional[int]: The positive export reference, or None when no
                matching export is found.
            """
            indices = self._export_ons_to_indices.get(bare)
            if not indices:
                return None
            if occurrence == 0:
                return indices[0] + 1
            if 1 <= occurrence <= len(indices):
                return indices[occurrence - 1] + 1
            return None

        if is_import is True:
            result = _lookup_imports()
            if result is not None:
                return result
        elif is_import is False:
            result = _lookup_exports()
            if result is not None:
                return result
        else:
            # Unprefixed fallback (for compatibility)
            result = _lookup_imports()
            if result is not None:
                return result
            result = _lookup_exports()
            if result is not None:
                return result
        try:
            return int(name)
        except (ValueError, TypeError):
            return 0

    # ------------------------------------------------------------------ #
    #  Reference resolution
    # ------------------------------------------------------------------ #

    def resolve(self) -> bool:
        """Resolve integer references on all imports and exports to items.

        Returns:
            bool: True on success; False after clearing resolved state on
            error.
        """
        self._invalidate_item_caches()
        try:
            for imp in self.imports:
                imp.resolve()
            for export in self.exports:
                export.resolve()
            return True
        except Exception:
            self.clear_resolved()
            return False

    def link(self) -> bool:
        """Reindex integer references on all imports and exports from items.

        Returns:
            bool: True on success; False after clearing resolved state on
            error.
        """
        try:
            for imp in self.imports:
                imp.link()
            for export in self.exports:
                export.link()
            return True
        except Exception:
            self.clear_resolved()
            return False

    def clear_resolved(self) -> None:
        """Unlink resolved item references on all imports and exports."""
        self._invalidate_item_caches()
        for imp in self.imports:
            imp.clear_resolved()
        for export in self.exports:
            export.clear_resolved()

    def clear_links(self) -> None:
        """Zero out integer reference properties on imports and exports.

        This is the inverse of :meth:`resolve`.  It resets ``group_index``
        (and ``class_index`` / ``super_index`` on exports) to 0 while leaving
        item pointers intact.
        """
        for imp in self.imports:
            imp.group_index = 0
        for export in self.exports:
            export.group_index = 0
            export.class_index = 0
            export.super_index = 0

    # ------------------------------------------------------------------ #
    #  Object creation & management
    # ------------------------------------------------------------------ #

    def create_objects(self) -> bool:
        """Create and parse the object for every export.

        Returns:
            bool: True when all objects were created and parsed; False on
            error.
        """
        try:
            for export in self.exports:
                export.create_object()
                if export.object is not None:
                    export.object.parse()
            self.objects_loaded = True
            return True
        except Exception:
            return False

    def resolve_objects(self) -> bool:
        """Populate item pointers on parsed objects from integer references.

        Returns:
            bool: True on success; False when objects are not loaded or after
            clearing linked objects on error.
        """
        if not self.objects_loaded:
            return False
        try:
            for export in self.exports:
                if export.object is not None:
                    export.object.resolve()
            return True
        except Exception:
            self.clear_linked_objects()
            return False

    def link_objects(self) -> bool:
        """Populate integer references on parsed objects from item pointers.

        Returns:
            bool: True on success; False when objects are not loaded or after
            clearing resolved objects on error.
        """
        if not self.objects_loaded:
            return False
        try:
            for export in self.exports:
                if export.object is not None:
                    export.object.link()
            return True
        except Exception:
            self.clear_resolved_objects()
            return False

    def clear_linked_objects(self) -> None:
        """Unlink resolved item pointers on every parsed export object."""
        if not self.objects_loaded:
            return
        for export in self.exports:
            if export.object is not None:
                export.object.clear_resolved()

    def clear_resolved_objects(self) -> None:
        """Zero out integer reference properties on parsed object items.

        This is the inverse of :meth:`resolve_objects`.  It resets index
        fields (e.g. ``super_index``, ``next_reference``,
        ``script_text_reference``, ``children_reference``) to 0 while leaving
        item pointers intact.
        """
        if not self.objects_loaded:
            return
        for export in self.exports:
            if export.object is not None:
                export.object.clear_links()
