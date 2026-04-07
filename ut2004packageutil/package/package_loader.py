"""Package loader with dependency resolution for Unreal packages.

Loads a primary package and recursively discovers and loads all dependency
packages using search paths from the UT2004 INI file.
"""

import os
from typing import Dict, List, Optional, Set

from ut2004packageutil.package.io import UnPackageIO
from ut2004packageutil.package.object import CODE_CLASS_NAMES
from ut2004packageutil.package.package import UnPackage
from ut2004packageutil.package.package_loader_global import (
    get_package_loader,
    set_package_loader,
)
from ut2004packageutil.utils.ini_utils import get_search_paths

# Re-export the registry helpers so existing import sites keep working.
__all__ = ["PackageLoader", "get_package_loader", "set_package_loader"]


class PackageLoader:
    """Coordinates loading of Unreal packages with dependency resolution.

    Parses ``[Core.System]`` ``Paths=`` entries from the INI file to build
    a list of search directories.  When loading a package, its import table
    is scanned for top-level ``Package``-class entries (each one names a
    referenced ``.u`` file), and each is loaded recursively.

    A dependency package that cannot be located on disk causes
    :meth:`load_with_dependencies` to raise ``FileNotFoundError`` — silently
    missing dependencies would let downstream code (notably the obfuscator)
    rewrite names that are actually external references.

    On construction the instance is automatically registered as the global
    singleton (accessible via :func:`get_package_loader`).
    """

    def __init__(self, ini_path: str, base_dir: Optional[str] = None) -> None:
        """Initialize the loader and register it as the global singleton.

        Args:
            ini_path (str): Path to the UT2004.ini (or equivalent)
                configuration file.
            base_dir (Optional[str]): If specified, search paths are resolved
                relative to base_dir first, then relative to the INI file's
                directory.
        """
        self.ini_path: str = os.path.abspath(ini_path)
        self.ini_dir: str = os.path.dirname(self.ini_path)
        self.base_dir: Optional[str] = os.path.abspath(base_dir) if base_dir else None
        self.loaded_packages: Dict[str, UnPackage] = {}
        self._loading_in_progress: Set[str] = set()  # circular dep guard

        # Build search path patterns from INI
        self._search_patterns: List[str] = get_search_paths(ini_path)

        # Auto-register as global singleton
        set_package_loader(self)

    def _resolve_search_dirs(self) -> List[str]:
        """Return fully resolved search directory patterns.

        If base_dir is set, its patterns come first (higher priority).
        Then the INI directory patterns.

        Returns:
            List[str]: The resolved and normalized search directory patterns.
        """
        dirs: List[str] = []
        if self.base_dir:
            for pattern in self._search_patterns:
                dirs.append(os.path.normpath(os.path.join(self.base_dir, pattern)))
        for pattern in self._search_patterns:
            dirs.append(os.path.normpath(os.path.join(self.ini_dir, pattern)))
        return dirs

    def find_package_file(self, package_name: str) -> Optional[str]:
        """Find the .u file for package_name using the search paths.

        Searches base_dir patterns first, then ini_dir patterns.  The
        match is case-insensitive on the basename so packages picked
        up from the import table (which may have been generated against
        a Windows install with a different spelling) still resolve on
        case-sensitive file systems.

        Args:
            package_name (str): The name of the package to locate (without
                extension).

        Returns:
            Optional[str]: The absolute path to the first matching file, or
                None if no match is found.
        """
        target = (package_name + ".u").lower()
        for pattern in self._resolve_search_dirs():
            # Pattern is like /path/to/System/*.u
            dir_path = os.path.dirname(pattern)
            ext = os.path.splitext(pattern)[1]  # e.g. ".u"
            # Fast path: exact-case match.
            candidate = os.path.join(dir_path, package_name + ext)
            if os.path.isfile(candidate):
                return candidate
            # Fallback: case-insensitive directory scan.
            if not os.path.isdir(dir_path):
                continue
            for entry in os.listdir(dir_path):
                if entry.lower() == target:
                    return os.path.join(dir_path, entry)
        return None

    def load_package(
        self,
        file_path: str,
        *,
        parse_objects: bool = True,
    ) -> UnPackage:
        """Load a single package from file_path.

        If parse_objects is False, only the header (names, imports,
        exports, references) is loaded — no object parsing.  This is used
        for dependency packages.

        Args:
            file_path (str): Path to the package file to load.
            parse_objects (bool): Whether to parse objects in the package.
                Defaults to True.

        Returns:
            UnPackage: The loaded package.
        """
        rdr = UnPackageIO()
        pkg = rdr.read_package(file_path)
        pkg.loader = self

        # Extract package name from filename
        pkg_name = os.path.splitext(os.path.basename(file_path))[0]
        self.loaded_packages[pkg_name] = pkg
        return pkg

    def load_with_dependencies(
        self,
        file_path: str,
        *,
        parse_objects: bool = True,
    ) -> UnPackage:
        """Load a package and all its dependencies recursively.

        The primary package gets full loading (including object parsing
        when parse_objects is True).  Dependencies are loaded
        header-only (no object parsing).

        Args:
            file_path (str): Path to the primary package file to load.
            parse_objects (bool): Whether to parse objects in the primary
                package. Defaults to True.

        Returns:
            UnPackage: The loaded primary package with its dependencies
                resolved.

        Raises:
            FileNotFoundError: When a dependency contributing code imports
                cannot be located on disk.
        """
        pkg_name = os.path.splitext(os.path.basename(file_path))[0]

        # Check cache
        if pkg_name in self.loaded_packages:
            return self.loaded_packages[pkg_name]

        # Circular dependency guard
        if pkg_name in self._loading_in_progress:
            return self.loaded_packages.get(pkg_name, UnPackage())
        self._loading_in_progress.add(pkg_name)

        try:
            # Load the package itself
            pkg = self.load_package(file_path, parse_objects=parse_objects)

            # Discover dependencies from imports.  The names of the
            # actual dependency packages live in the import table as
            # entries with ``class_name == "Package"`` and no group
            # (top-level).  The ``class_package_name`` field on every
            # import points at "Core" (the package that defines the
            # ``Package`` class), which is *not* the package the
            # imported item actually lives in.
            #
            # While we walk the table we also record, per dependency, the
            # object-class names of the items imported *from* it.  This
            # lets us tell a code package (something we import a Class /
            # Struct / Function / property from — see ``CODE_CLASS_NAMES``)
            # apart from a content-only package (only Texture / Sound /
            # mesh / material references).  A content-only package that
            # cannot be located on disk is replaced with a placeholder
            # instead of aborting the load.
            dep_names: Set[str] = set()
            dep_import_classes: Dict[str, Set[str]] = {}
            for imp in pkg.imports:
                if imp.group_item is None:
                    if imp.class_name is not None and imp.class_name.name == "Package":
                        dep_name = imp.object_name.name
                        if dep_name and dep_name != pkg_name:
                            dep_names.add(dep_name)
                    continue
                # A nested import — attribute its class to its root package.
                root = self._root_package_name(imp)
                if root and root != pkg_name:
                    cls = imp.class_name.name if imp.class_name is not None else ""
                    dep_import_classes.setdefault(root, set()).add(cls)

            # Load each dependency.  A missing dependency is fatal *unless*
            # every item imported from it is content (Texture/Sound/mesh/
            # material): such a package contributes no code, so a placeholder
            # stands in for it — its imports are never dereferenced and keep
            # their original reference.
            for dep_name in sorted(dep_names):
                if dep_name in self.loaded_packages:
                    pkg.imported_packages[dep_name] = self.loaded_packages[dep_name]
                    continue
                dep_path = self.find_package_file(dep_name)
                if dep_path is not None:
                    dep_pkg = self.load_with_dependencies(dep_path, parse_objects=False)
                    pkg.imported_packages[dep_name] = dep_pkg
                    continue
                # File not found — only tolerable for a content-only package.
                imported_classes = dep_import_classes.get(dep_name, set())
                code_imports = imported_classes & CODE_CLASS_NAMES
                if code_imports:
                    raise FileNotFoundError(
                        f"Could not locate dependency package {dep_name!r} "
                        f"referenced by {pkg_name!r}.  It contributes code "
                        f"imports ({', '.join(sorted(code_imports))}) so a "
                        f"placeholder cannot stand in for it.  Searched: "
                        f"{', '.join(self._resolve_search_dirs())}"
                    )
                placeholder = self._make_placeholder_package(dep_name)
                self.loaded_packages[dep_name] = placeholder
                pkg.imported_packages[dep_name] = placeholder

            # The primary package's objects were resolved inside load_package()
            # above — before imported_packages was wired.  Tagged-property
            # object-reference capture for inherited array<Object>/array<Class>
            # members needs the import graph to determine an array's inner type;
            # without it the walk silently skips those refs and they are never
            # re-linked on write (breaking any table renumbering).  Now
            # that dependencies are in place, re-resolve so the capture is
            # complete.  This is idempotent for references already resolved.
            if parse_objects and pkg.objects_loaded:
                pkg.resolve_objects()

            return pkg
        finally:
            self._loading_in_progress.discard(pkg_name)

    @staticmethod
    def _root_package_name(item: "object") -> str:
        """Return the name of the top-level package an import lives under.

        Imports are organised hierarchically: the top of an import's
        ``group_item`` chain is a top-level ``Package``-class entry whose
        ``object_name`` names the source ``.u`` file.  Walking to the top
        gives the package the item is actually imported from (as opposed to
        ``class_package_name``, which names the package defining the item's
        *class*).

        Args:
            item ("object"): The import table entry to walk up from.

        Returns:
            str: The name of the top-level package, or an empty string if
                none can be determined.
        """
        cur = item
        while getattr(cur, "group_item", None) is not None:
            cur = cur.group_item
        name_entry = getattr(cur, "object_name", None)
        return name_entry.name if name_entry is not None else ""

    def _make_placeholder_package(self, name: str) -> UnPackage:
        """Build an empty placeholder package standing in for name.

        Used when a content-only dependency (one we import only
        Texture/Sound/… from) cannot be located on disk.  The placeholder
        has no names, imports, or exports, so any import that targets it
        resolves its ``object`` to ``None`` and is never dereferenced.

        Args:
            name (str): The name of the missing package to stand in for.

        Returns:
            UnPackage: An empty placeholder package.
        """
        placeholder = UnPackage()
        placeholder.name = name
        placeholder.loader = self
        placeholder.is_placeholder = True
        return placeholder

    def get_package(self, name: str) -> Optional[UnPackage]:
        """Return an already-loaded package by name, or None.

        Args:
            name (str): The name of the package to retrieve.

        Returns:
            Optional[UnPackage]: The loaded package, or None if not loaded.
        """
        return self.loaded_packages.get(name)
