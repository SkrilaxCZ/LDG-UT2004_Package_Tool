"""Unreal package binary I/O and XML serialisation."""

import os
import xml.etree.ElementTree as ET
from typing import Any, BinaryIO, Dict, List, Optional

from ut2004packageutil.package.flags import (
    UnGuid,
    UnObjectFlags,
    UnPackageFlags,
    object_flags_to_string,
    package_flags_to_string,
    string_to_object_flags,
    string_to_package_flags,
)
from ut2004packageutil.package.package import (
    UnExport,
    UnGeneration,
    UnImport,
    UnName,
    UnPackage,
    UnPackageItem,
)
from ut2004packageutil.package.package_loader_global import get_package_loader
from ut2004packageutil.package.xml_utils import dict_to_xml, xml_to_dict
from ut2004packageutil.utils.io_utils import read_index, write_index
from ut2004packageutil.utils.struct_utils import (
    read_int,
    read_uint,
    write_int,
    write_uint,
)


class UnPackageIO:
    """Reads and writes Unreal .u package files."""

    def __init__(self) -> None:
        """Initialise the I/O helper with no open package stream."""
        self._package_stream: Optional[BinaryIO] = None

    @property
    def package_stream(self) -> Optional[BinaryIO]:
        """Return the currently open package stream.

        Returns:
            Optional[BinaryIO]: The open stream, or ``None`` if none is open.
        """
        return self._package_stream

    # ================================================================= #
    #  Reading
    # ================================================================= #

    @staticmethod
    def _read_names(
        pkg: "UnPackage", reader: BinaryIO, offset: int, count: int
    ) -> None:
        """Read the name table from the stream into the package.

        Args:
            pkg ("UnPackage"): The package to populate with names.
            reader (BinaryIO): The binary stream to read from.
            offset (int): Byte offset of the name table.
            count (int): Number of name entries to read.
        """
        reader.seek(offset)
        for _ in range(count):
            name_length = read_index(reader)
            if name_length == 0:
                name = ""
            else:
                raw = reader.read(name_length)
                name = raw.decode("latin-1").rstrip("\x00")
            flags = read_int(reader)
            pkg.add_name(name, UnObjectFlags(flags))

    @staticmethod
    def _read_exports(
        pkg: "UnPackage", reader: BinaryIO, offset: int, count: int
    ) -> None:
        """Read the export table from the stream into the package.

        Args:
            pkg ("UnPackage"): The package to populate with exports.
            reader (BinaryIO): The binary stream to read from.
            offset (int): Byte offset of the export table.
            count (int): Number of export entries to read.
        """
        reader.seek(offset)
        for _ in range(count):
            cls = read_index(reader)
            super_ref = read_index(reader)
            group = read_int(reader)
            name = read_index(reader)
            flags = read_uint(reader)
            exp_size = read_index(reader)
            exp_offset = read_index(reader) if exp_size > 0 else 0
            pkg.add_export(
                name, group, cls, super_ref, UnObjectFlags(flags), exp_offset, exp_size
            )

    @staticmethod
    def _read_imports(
        pkg: "UnPackage", reader: BinaryIO, offset: int, count: int
    ) -> None:
        """Read the import table from the stream into the package.

        Args:
            pkg ("UnPackage"): The package to populate with imports.
            reader (BinaryIO): The binary stream to read from.
            offset (int): Byte offset of the import table.
            count (int): Number of import entries to read.
        """
        reader.seek(offset)
        for _ in range(count):
            cls_pkg = read_index(reader)
            cls_name = read_index(reader)
            group = read_int(reader)
            name = read_index(reader)
            pkg.add_import(group, cls_pkg, cls_name, name)

    def _read_exports_binary(self, pkg: "UnPackage", reader: BinaryIO) -> None:
        """Read the raw binary data for each export from the stream.

        Args:
            pkg ("UnPackage"): The package whose exports are populated.
            reader (BinaryIO): The binary stream to read from.
        """
        for exp in pkg.exports:
            if exp.export_size > 0:
                reader.seek(exp.export_offset)
                exp.export_data = reader.read(exp.export_size)
                exp.original_data_offset = exp.export_offset
                exp.export_offset = -1
                exp.export_size = -1

    def read_package(self, file_name: str) -> "UnPackage":
        """Read an Unreal ``.u`` package file from disk.

        Args:
            file_name (str): Path to the package file to read.

        Returns:
            "UnPackage": The fully parsed and resolved package.

        Raises:
            RuntimeError: When the magic header is invalid, or a reference,
                object, or object reference cannot be resolved.
        """
        reader = None
        try:
            self._package_stream = open(file_name, "rb")
            reader = self._package_stream
            pkg = UnPackage()
            pkg.name = file_name.replace(".u", "")

            magic = read_uint(reader)
            if magic != UnPackage.MAGIC_HEADER:
                raise RuntimeError("Invalid magic.")

            ver = read_int(reader)
            pkg.version = ver & 0xFFFF
            pkg.licensee_version = (ver >> 16) & 0xFFFF

            flags = read_uint(reader)
            pkg.flags = UnPackageFlags(flags)

            name_count = read_int(reader)
            name_offset = read_int(reader)
            export_count = read_int(reader)
            export_offset = read_int(reader)
            import_count = read_int(reader)
            import_offset = read_int(reader)

            pkg.guid = UnGuid.from_stream(reader)

            generation_count = read_int(reader)
            for _ in range(generation_count):
                g_exp_count = read_int(reader)
                g_name_count = read_int(reader)
                pkg.add_generation(g_exp_count, g_name_count)

            self._read_names(pkg, reader, name_offset, name_count)
            self._read_imports(pkg, reader, import_offset, import_count)
            self._read_exports(pkg, reader, export_offset, export_count)
            self._read_exports_binary(pkg, reader)

            # Set generation boundaries from the last generation entry
            if pkg.generations:
                last_gen = pkg.generations[-1]
                pkg.names.last_generation_count = last_gen.name_count
                pkg.exports.last_generation_count = last_gen.export_count

            if not pkg.resolve():
                raise RuntimeError("Invalid reference.")
            if not pkg.create_objects():
                raise RuntimeError("Invalid object.")
            if not pkg.resolve_objects():
                raise RuntimeError("Invalid object reference.")

            reader.close()
            self._package_stream = None
            return pkg
        except Exception:
            if reader is not None:
                reader.close()
            self._package_stream = None
            raise

    # ================================================================= #
    #  Writing
    # ================================================================= #

    @staticmethod
    def _write_names(pkg: "UnPackage", writer: BinaryIO) -> None:
        """Write the package name table to the stream.

        Args:
            pkg ("UnPackage"): The package whose names are written.
            writer (BinaryIO): The binary stream to write to.
        """
        for name_entry in pkg.names:
            name_bin = name_entry.name.encode("latin-1")
            write_index(writer, len(name_bin) + 1)
            writer.write(name_bin)
            writer.write(b"\x00")
            write_uint(writer, int(name_entry.flags))

    @staticmethod
    def _write_export_data(pkg: "UnPackage", writer: BinaryIO) -> None:
        """Serialize and write each export's binary data to the stream.

        Args:
            pkg ("UnPackage"): The package whose export data is written.
            writer (BinaryIO): The binary stream to write to.
        """
        from ut2004packageutil.package.object import patch_texture_lazy_offsets

        for export in pkg.exports:
            if export.object is not None:
                export.object.serialize(writer.tell())
            if export.export_data is not None:
                export.export_offset = writer.tell()
                export.export_size = len(export.export_data)
                data = export.export_data
                # Unparsed Texture blobs embed absolute file offsets in their
                # mip TLazyArrays; if the object moved, shift them so they stay
                # valid.  Patch a copy — export_data keeps the original offsets
                # so repeated writes stay correct.
                if (
                    export.object is None
                    and export.original_data_offset >= 0
                    and export.class_name_string.endswith(".Texture")
                ):
                    delta = export.export_offset - export.original_data_offset
                    if delta:
                        data = patch_texture_lazy_offsets(data, delta, pkg)
                writer.write(data)
            else:
                export.export_size = 0
                export.export_offset = 0

    @staticmethod
    def _write_imports(pkg: "UnPackage", writer: BinaryIO) -> None:
        """Write the package import table to the stream.

        Args:
            pkg ("UnPackage"): The package whose imports are written.
            writer (BinaryIO): The binary stream to write to.
        """
        for imp in pkg.imports:
            write_index(writer, pkg.name_index(imp.class_package_name))
            write_index(writer, pkg.name_index(imp.class_name))
            write_int(writer, imp.group_index)
            write_index(writer, pkg.name_index(imp.object_name))

    @staticmethod
    def _write_exports(pkg: "UnPackage", writer: BinaryIO) -> None:
        """Write the package export table to the stream.

        Args:
            pkg ("UnPackage"): The package whose exports are written.
            writer (BinaryIO): The binary stream to write to.
        """
        for export in pkg.exports:
            write_index(writer, export.class_index)
            write_index(writer, export.super_index)
            write_int(writer, export.group_index)
            write_index(writer, pkg.name_index(export.object_name))
            write_uint(writer, int(export.flags))
            write_index(writer, export.export_size)
            if export.export_size > 0:
                write_index(writer, export.export_offset)
            export.export_size = -1
            export.export_offset = -1

    def write_package(self, pkg: "UnPackage", file_name: str) -> None:
        """Write a package to an Unreal ``.u`` file on disk.

        Args:
            pkg ("UnPackage"): The package to serialize.
            file_name (str): Path of the file to write.

        Raises:
            RuntimeError: When references or object references cannot be
                reindexed.
        """
        writer = None
        try:
            writer = open(file_name, "wb")
            write_uint(writer, UnPackage.MAGIC_HEADER)
            ver = (pkg.version & 0xFFFF) + ((pkg.licensee_version & 0xFFFF) << 16)
            write_int(writer, ver)
            write_uint(writer, int(pkg.flags))

            write_int(writer, len(pkg.names))
            names_stream_pos = writer.tell()
            write_int(writer, 0)

            write_int(writer, len(pkg.exports))
            export_stream_pos = writer.tell()
            write_int(writer, 0)

            write_int(writer, len(pkg.imports))
            import_stream_pos = writer.tell()
            write_int(writer, 0)

            # Preserve existing generations; add a new one only if counts changed
            if not pkg.generations:
                # No generations at all — create one and regenerate GUID
                pkg.generations.append(UnGeneration(len(pkg.exports), len(pkg.names)))
                pkg.guid = UnGuid.generate()
            else:
                last_gen = pkg.generations[-1]
                if last_gen.export_count != len(
                    pkg.exports
                ) or last_gen.name_count != len(pkg.names):
                    pkg.generations.append(
                        UnGeneration(len(pkg.exports), len(pkg.names))
                    )

            pkg.guid.write(writer)

            write_int(writer, len(pkg.generations))
            for gen in pkg.generations:
                write_int(writer, gen.export_count)
                write_int(writer, gen.name_count)

            cur_pos = writer.tell()
            writer.seek(names_stream_pos)
            write_int(writer, cur_pos)
            writer.seek(cur_pos)

            if not pkg.link():
                raise RuntimeError("Could not reindex references.")
            if not pkg.link_objects():
                raise RuntimeError("Could not reindex object references.")

            self._write_names(pkg, writer)
            self._write_export_data(pkg, writer)

            cur_pos = writer.tell()
            writer.seek(import_stream_pos)
            write_int(writer, cur_pos)
            writer.seek(cur_pos)
            self._write_imports(pkg, writer)

            cur_pos = writer.tell()
            writer.seek(export_stream_pos)
            write_int(writer, cur_pos)
            writer.seek(cur_pos)
            self._write_exports(pkg, writer)

            writer.close()
        except Exception:
            if writer is not None:
                writer.close()
            raise

    # ================================================================= #
    #  XML Export / Import
    # ================================================================= #

    def export_xml(
        self, pkg: "UnPackage", output_dir: str, drop_generations: bool = False
    ) -> None:
        """Export the package as a directory with ``Package.xml`` and sidecars.

        For ``UnTextBuffer`` exports the ``script_text`` content is written to
        a separate ``.txt`` file under an ``UnTextBuffer/`` subdirectory using
        the fully qualified export name (e.g. ``Canvas.uc.txt``). The XML
        stores only the filename reference instead of the full text. All other
        export data stays in the XML as before.

        Args:
            pkg ("UnPackage"): The package to export.
            output_dir (str): Directory to write the XML and sidecar files to.
            drop_generations (bool): Whether to drop generations before export.
        """

        os.makedirs(output_dir, exist_ok=True)

        if drop_generations:
            pkg.drop_generations()

        root = ET.Element("Package")
        ET.SubElement(root, "Version").text = str(pkg.version)
        ET.SubElement(root, "LicenseeVersion").text = str(pkg.licensee_version)
        ET.SubElement(root, "Flags").text = package_flags_to_string(pkg.flags)
        ET.SubElement(root, "GUID").text = (
            pkg.guid.to_hex() if pkg.guid and not pkg.guid.is_empty() else ""
        )

        gens_el = ET.SubElement(root, "Generations")
        for gen in pkg.generations:
            gen_el = ET.SubElement(gens_el, "Generation")
            gen_el.set("ExportCount", str(gen.export_count))
            gen_el.set("NameCount", str(gen.name_count))

        names_el = ET.SubElement(root, "Names")
        for name_entry in pkg.names:
            el = ET.SubElement(names_el, "Name")
            el.set("Name", name_entry.name)
            el.set("Flags", object_flags_to_string(name_entry.flags))

        imports_el = ET.SubElement(root, "Imports")
        for imp in pkg.imports:
            el = ET.SubElement(imports_el, "Import")
            el.set("Name", pkg.resolve_name_index(pkg.name_index(imp.object_name)))
            el.set("Group", pkg.resolve_item_ref(imp.group_index))
            el.set(
                "Class",
                pkg.resolve_name_index(pkg.name_index(imp.class_name))
                if imp.class_name
                else "",
            )
            el.set(
                "Package",
                pkg.resolve_name_index(pkg.name_index(imp.class_package_name))
                if imp.class_package_name
                else "",
            )

        exports_el = ET.SubElement(root, "Exports")
        for export in pkg.exports:
            el = ET.SubElement(exports_el, "Export")
            el.set("Name", pkg.resolve_name_index(pkg.name_index(export.object_name)))
            el.set("Flags", object_flags_to_string(export.flags))
            el.set("Group", pkg.resolve_item_ref(export.group_index))
            el.set("Class", pkg.resolve_item_ref(export.class_index))
            el.set("Super", pkg.resolve_item_ref(export.super_index))
            if export.object is not None:
                obj_dict = export.object.to_dict()

                # Let object handle sidecar files (e.g. UnTextBuffer)
                export.object.export_xml(obj_dict, output_dir)

                data_el = ET.SubElement(el, "ObjectData")
                dict_to_xml(data_el, obj_dict)
            elif export.export_data is not None:
                # Write binary data as sidecar .bin file in Raw-prefixed class-named folder
                class_name = (
                    export.class_name_string.split(".")[-1]
                    if export.class_name_string
                    else "Unknown"
                )
                folder_name = "Raw" + class_name
                bin_filename = export.object_name_string + ".bin"
                bin_subdir = os.path.join(output_dir, folder_name)
                os.makedirs(bin_subdir, exist_ok=True)
                bin_path = os.path.join(bin_subdir, bin_filename)
                with open(bin_path, "wb") as bf:
                    bf.write(export.export_data)
                bin_el = ET.SubElement(el, "BinaryFile")
                bin_el.set("folder", folder_name)
                bin_el.set("file", bin_filename)

        ET.indent(root, space="  ")
        tree = ET.ElementTree(root)
        xml_path = os.path.join(output_dir, "Package.xml")
        tree.write(xml_path, encoding="unicode", xml_declaration=True)

    def import_xml(self, input_dir: str) -> "UnPackage":
        """Import a package from a directory with ``Package.xml`` and sidecars.

        Creates and returns a new :class:`UnPackage`. For exports with
        ``<ObjectData>``, uses ``from_dict()`` to populate the object. For
        ``UnTextBuffer`` exports the ``script_text_file`` entry is resolved to
        a ``.txt`` file under the ``UnTextBuffer/`` subdirectory. For exports
        with ``<HexData>``, converts the hex text back to bytes for
        ``export_data``. Uses the global :func:`get_package_loader` singleton
        for dependency resolution. If no loader is registered, dependencies
        are skipped.

        Args:
            input_dir (str): Directory containing ``Package.xml`` and sidecars.

        Returns:
            "UnPackage": The imported package.

        Raises:
            ValueError: When the input directory or ``Package.xml`` is missing.
            RuntimeError: When package version, licensee version, GUID,
                generations, names, imports, exports, or references are
                invalid or cannot be resolved.
        """
        pkg = UnPackage()
        loader = get_package_loader()
        if loader is not None:
            pkg.loader = loader
        if not os.path.isdir(input_dir):
            raise ValueError(f"Input directory not found: {input_dir}")

        xml_path = os.path.join(input_dir, "Package.xml")
        if not os.path.isfile(xml_path):
            raise ValueError(f"Package.xml not found in: {input_dir}")

        pkg.reset_generations()
        pkg.names.clear()
        pkg.imports.clear()
        pkg.exports.clear()

        tree = ET.parse(xml_path)
        root = tree.getroot()

        name_el = root.find("Name")
        if name_el is not None and name_el.text:
            pkg.name = name_el.text
        else:
            # Derive name from directory name
            pkg.name = os.path.basename(os.path.normpath(input_dir))

        version_el = root.find("Version")
        if version_el is None:
            raise RuntimeError("Invalid package version.")
        pkg.version = int(version_el.text)

        lm_el = root.find("LicenseeVersion")
        if lm_el is None:
            # Backward compat: try old name
            lm_el = root.find("LicenseeMode")
        if lm_el is None:
            raise RuntimeError("Invalid package licensee version.")
        pkg.licensee_version = int(lm_el.text)

        flags_el = root.find("Flags")
        pkg.flags = string_to_package_flags(
            flags_el.text if flags_el is not None and flags_el.text else ""
        )

        guid_el = root.find("GUID")
        guid_text = guid_el.text if guid_el is not None and guid_el.text else ""

        # Read generations
        gens_el = root.find("Generations")
        has_generations = False
        if gens_el is not None:
            for gen_el in gens_el.findall("Generation"):
                exp_count = int(gen_el.get("ExportCount", "0"))
                name_count = int(gen_el.get("NameCount", "0"))
                pkg.generations.append(UnGeneration(exp_count, name_count))
                has_generations = True

        # Validate GUID vs generations consistency
        if guid_text and has_generations:
            # Both present — validate GUID
            if len(guid_text) != 32:
                raise RuntimeError("GUID length must be 32.")
            my_guid = guid_text.upper()
            for c in my_guid:
                if c not in "0123456789ABCDEF":
                    raise RuntimeError(f"GUID is not valid - invalid character {c}.")
            pkg.guid = UnGuid.from_hex(my_guid)
        elif not guid_text and not has_generations:
            # Both empty — will be regenerated during write
            pkg.guid = UnGuid()
        elif not guid_text and has_generations:
            raise RuntimeError("Empty GUID with non-empty Generations is not allowed.")
        else:  # guid_text and not has_generations
            raise RuntimeError("Non-empty GUID with empty Generations is not allowed.")

        names_el = root.find("Names")
        if names_el is not None:
            for name_item in names_el.findall("Name"):
                n = name_item.get("Name", "")
                if not n:
                    raise RuntimeError("Empty name in name table.")
                f = name_item.get("Flags", "")
                pkg.names.append(UnName(n, string_to_object_flags(f)))

        # Validate: if no generations, check that names are unique (deduplicated)
        if not has_generations:
            seen_names: Dict[str, int] = {}
            for name_item in names_el.findall("Name") if names_el is not None else []:
                n = name_item.get("Name", "")
                seen_names[n] = seen_names.get(n, 0) + 1
            for n, count in seen_names.items():
                if count > 1:
                    raise RuntimeError(
                        f"Duplicate name '{n}' found {count} times in name table, "
                        f"but no generations are present (names should be deduplicated)."
                    )

        imports_el = root.find("Imports")
        if imports_el is not None:
            for imp_item in imports_el.findall("Import"):
                import_name_ref = imp_item.get("Name", "")
                if not import_name_ref:
                    raise RuntimeError("Empty name in imports table.")
                my_import_name = pkg.find_name_by_ref(import_name_ref)
                if my_import_name is None:
                    raise RuntimeError(
                        f"The import name {import_name_ref} is not in the names table."
                    )

                import_group = imp_item.get("Group", "")
                import_class_ref = imp_item.get("Class", "")
                if not import_class_ref:
                    raise RuntimeError(f"Empty class for import {import_name_ref}.")
                my_import_class = pkg.find_name_by_ref(import_class_ref)
                if my_import_class is None:
                    raise RuntimeError(
                        f"The import class {import_class_ref} for {import_name_ref} is not in the names table."
                    )

                import_package_ref = imp_item.get("Package", "")
                if not import_package_ref:
                    raise RuntimeError(f"Empty package for {import_name_ref}.")
                my_import_package = pkg.find_name_by_ref(import_package_ref)
                if my_import_package is None:
                    raise RuntimeError(
                        f"The import package {import_package_ref} for {import_name_ref} is not in the names table."
                    )

                my_import = UnImport(
                    pkg,
                    my_import_name,
                    group_item=None,
                    class_package_name=my_import_package,
                    class_name=my_import_class,
                )
                my_import.group_data = import_group
                pkg.imports.append(my_import)

        # Collect per-export ObjectData dicts for deferred from_dict() loading
        export_object_dicts: List[Optional[Dict[str, Any]]] = []

        exports_el = root.find("Exports")
        if exports_el is not None:
            for exp_item in exports_el.findall("Export"):
                export_name_ref = exp_item.get("Name", "")
                if not export_name_ref:
                    raise RuntimeError("Empty name in exports table.")

                my_export_name = pkg.find_name_by_ref(export_name_ref)
                if my_export_name is None:
                    raise RuntimeError(
                        f"The export name {export_name_ref} is not in the names table."
                    )

                my_export_flags_str = exp_item.get("Flags", "")
                export_group = exp_item.get("Group", "")
                export_class = exp_item.get("Class", "")
                export_super = exp_item.get("Super", "")

                my_export_binary: Optional[bytes] = None
                obj_data_dict: Optional[Dict[str, Any]] = None

                # Check for ObjectData, BinaryFile
                obj_data_el = exp_item.find("ObjectData")
                bin_file_el = exp_item.find("BinaryFile")

                if obj_data_el is not None:
                    obj_data_dict = xml_to_dict(obj_data_el)

                    # Provide placeholder export_data so create_object can work;
                    # it will be replaced by serialize() after from_dict()
                    my_export_binary = b""
                elif bin_file_el is not None:
                    folder = bin_file_el.get("folder", "")
                    filename = bin_file_el.get("file", "")
                    bin_path = os.path.join(input_dir, folder, filename)
                    with open(bin_path, "rb") as bf:
                        my_export_binary = bf.read()

                my_export = UnExport(
                    pkg,
                    my_export_name,
                    group_item=None,
                    class_item=None,
                    super_item=None,
                    flags=string_to_object_flags(my_export_flags_str),
                    export_data=my_export_binary,
                )
                my_export.group_data = export_group
                my_export.class_data = export_class
                my_export.super_data = export_super
                pkg.exports.append(my_export)
                export_object_dicts.append(obj_data_dict)

        def _find_prefixed_item(
            ref_str: str, resolved_only: bool = False
        ) -> Optional["UnPackageItem"]:
            """Resolve a prefixed reference string to an item.

            ``-Name`` searches imports, ``+Name`` searches exports, bare
            ``Name`` searches imports then exports. ``#N`` suffix selects the
            N-th occurrence (1-based). When *resolved_only* is ``True``, only
            items whose own group has already been resolved are considered
            (their ``object_name_string`` is final).

            Args:
                ref_str (str): The prefixed reference string to resolve.
                resolved_only (bool): Whether to consider only items whose
                    group is already resolved.

            Returns:
                Optional["UnPackageItem"]: The matching item, or ``None``.
            """
            if not ref_str:
                return None

            # Parse prefix
            if ref_str.startswith("-") or ref_str.startswith("+"):
                is_import = ref_str.startswith("-")
                bare = ref_str[1:]
            else:
                is_import = None
                bare = ref_str

            # Parse #N occurrence suffix
            occurrence = 0  # 0 = first match
            if "#" in bare:
                bare, suffix = bare.rsplit("#", 1)
                try:
                    occurrence = int(suffix)
                except (ValueError, TypeError):
                    pass

            def _search_imports() -> Optional["UnPackageItem"]:
                """Search the import table for a matching item.

                Returns:
                    Optional["UnPackageItem"]: The matching import, or ``None``.
                """
                current = 0
                for imp in pkg.imports:
                    if resolved_only and imp.group_data:
                        continue
                    if imp.object_name_string == bare:
                        current += 1
                        if occurrence == 0 or current == occurrence:
                            return imp
                return None

            def _search_exports() -> Optional["UnPackageItem"]:
                """Search the export table for a matching item.

                Returns:
                    Optional["UnPackageItem"]: The matching export, or ``None``.
                """
                current = 0
                for exp in pkg.exports:
                    if resolved_only and exp.group_data:
                        continue
                    if exp.object_name_string == bare:
                        current += 1
                        if occurrence == 0 or current == occurrence:
                            return exp
                return None

            if is_import is True:
                return _search_imports()
            elif is_import is False:
                return _search_exports()
            else:
                result = _search_imports()
                if result is not None:
                    return result
                return _search_exports()

        # Resolve import groups (iterative for hierarchical resolution)
        while True:
            resolved = 0
            for imp in pkg.imports:
                if imp.group_data and imp.group_item is None:
                    item = _find_prefixed_item(imp.group_data, resolved_only=True)
                    if item is not None:
                        imp.group_item = item
                        imp.group_data = ""
                        resolved += 1
            if resolved == 0:
                break

        # Resolve export groups (iterative for hierarchical resolution)
        while True:
            resolved = 0
            for export in pkg.exports:
                if export.group_data and export.group_item is None:
                    item = _find_prefixed_item(export.group_data, resolved_only=True)
                    if item is not None:
                        export.group_item = item
                        export.group_data = ""
                        resolved += 1
            if resolved == 0:
                break

        for imp in pkg.imports:
            if imp.group_data:
                raise RuntimeError(
                    f"Import {imp.object_name_string} does not have group reference resolved ({imp.group_data})."
                )
        for export in pkg.exports:
            if export.group_data:
                raise RuntimeError(
                    f"Export {export.object_name_string} does not have group reference resolved ({export.group_data})."
                )

        for export in pkg.exports:
            if export.class_data:
                export.class_item = _find_prefixed_item(export.class_data)
                if export.class_item is None:
                    raise RuntimeError(
                        f"Could not find class reference {export.class_data} for {export.object_name_string}."
                    )
                export.class_data = ""
            if export.super_data:
                export.super_item = _find_prefixed_item(export.super_data)
                if export.super_item is None:
                    raise RuntimeError(
                        f"Could not find super reference {export.super_data} for {export.object_name_string}."
                    )
                export.super_data = ""
                export.super_item.children.append(export)

        if not pkg.link():
            raise RuntimeError("Failed in reindexing the references.")

        # Load dependency packages if a loader is available
        if loader is not None:
            dep_names = set()
            for imp in pkg.imports:
                if imp.class_package_name is not None:
                    dep_name = imp.class_package_name.name
                    if dep_name and dep_name != pkg.name:
                        dep_names.add(dep_name)
            for dep_name in dep_names:
                if dep_name in loader.loaded_packages:
                    pkg.imported_packages[dep_name] = loader.loaded_packages[dep_name]
                else:
                    dep_path = loader.find_package_file(dep_name)
                    if dep_path is not None:
                        dep_pkg = loader.load_with_dependencies(
                            dep_path, parse_objects=True
                        )
                        pkg.imported_packages[dep_name] = dep_pkg
            # Register this package in loader too
            loader.loaded_packages[pkg.name] = pkg

        # Create objects, then populate from dict data where available.
        # Two passes: first pre-populate every UnField's integer references
        # (super/next/children) so cross-references between structs/classes
        # resolve correctly when later exports' tagged-property data needs
        # to look up a struct's field list (e.g. to recover an omitted
        # ``type`` attribute on a ``<Field>`` element).  Then run the full
        # from_dict on every export.
        for export in pkg.exports:
            export.create_object()
        for i, export in enumerate(pkg.exports):
            obj = export.object
            if obj is None:
                continue
            obj_dict = export_object_dicts[i] if i < len(export_object_dicts) else None
            # Only UnField subclasses carry super/next references — detect via
            # duck typing to avoid importing the concrete class (would create
            # an import cycle: io ↔ object).
            if (
                obj_dict is None
                or not hasattr(obj, "super_index")
                or not hasattr(obj, "next_reference")
            ):
                continue
            # Set super/next references (UnField) and children (UnStruct+).
            obj.super_index = obj._link_object_ref(obj_dict.get("super", ""))
            obj.next_reference = obj._link_object_ref(obj_dict.get("next", ""))
            if hasattr(obj, "children_reference"):
                obj.children_reference = obj._link_object_ref(
                    obj_dict.get("children", obj_dict.get("children_ref", ""))
                )
            # UnObjectProperty / UnClassProperty / UnStructProperty etc.
            # also need their type-link references resolved up-front so
            # _find_array_inner_info can follow them.
            for attr in (
                "inner_reference",
                "struct_reference",
                "property_class_reference",
                "meta_class_reference",
                "enum_reference",
                "key_reference",
                "value_reference",
                "function_reference",
            ):
                if hasattr(obj, attr):
                    ref_key = attr.replace("_reference", "")
                    if ref_key == "property_class":
                        ref_key = "property_class"
                    elif ref_key == "meta_class":
                        ref_key = "meta_class"
                    setattr(obj, attr, obj._link_object_ref(obj_dict.get(ref_key, "")))
        for i, export in enumerate(pkg.exports):
            if export.object is not None:
                obj_dict = (
                    export_object_dicts[i] if i < len(export_object_dicts) else None
                )
                if obj_dict is not None:
                    export.object.import_xml(obj_dict, input_dir)
                    export.object.from_dict(obj_dict)
                else:
                    export.object.parse()
        pkg.objects_loaded = True

        # Populate item pointers from the integer references set by from_dict(),
        # so that resolve_objects() can correctly convert them back.
        if not pkg.resolve_objects():
            raise RuntimeError(
                "Failed to dereference object references after from_dict()."
            )

        if not pkg.link_objects():
            raise RuntimeError("Failed in reindexing the exports' objects' references.")

        return pkg
