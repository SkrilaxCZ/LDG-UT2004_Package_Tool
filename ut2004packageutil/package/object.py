"""Unreal package object types and utility functions."""

import io
import os
from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Callable,
    Dict,
    List,
    Optional,
    Type,
)

from ut2004packageutil.package.flags import (
    UnClassFlags,
    UnFunctionFlags,
    UnGuid,
    UnNameMap,
    UnObjectFlags,
    UnPropertyFlags,
    UnStateFlags,
    UnStructFlags,
    class_flags_to_string,
    function_flags_to_string,
    ignore_mask_to_string,
    probe_mask_to_string,
    property_flags_to_string,
    state_flags_to_string,
    string_to_class_flags,
    string_to_function_flags,
    string_to_ignore_mask,
    string_to_probe_mask,
    string_to_property_flags,
    string_to_state_flags,
    string_to_struct_flags,
    struct_flags_to_string,
)
from ut2004packageutil.package.package import (
    link_item,
    register_object_factory,
    resolve_item,
)
from ut2004packageutil.package.token import TokenStreamParser
from ut2004packageutil.package.token_asm import asm_to_tokens, tokens_to_asm
from ut2004packageutil.structs import UnString as _UnString
from ut2004packageutil.utils.io_utils import (
    bytes_to_hex,
    hex_to_bytes,
    read_ascii,
    read_index,
    write_ascii,
    write_index,
)
from ut2004packageutil.utils.struct_utils import (
    format_float32,
    pack_byte,
    pack_float,
    pack_int,
    read_byte,
    read_float,
    read_int,
    read_uint,
    read_ulong,
    read_word,
    unpack_float,
    unpack_int,
    write_byte,
    write_float,
    write_int,
    write_uint,
    write_ulong,
    write_word,
)

if TYPE_CHECKING:
    # Imported for type annotations only. ``package.py`` does not import this
    # module at import time, but these classes are referenced solely in quoted
    # annotations, so a TYPE_CHECKING-guarded import avoids any runtime cycle.
    from ut2004packageutil.package.package import (
        UnExport,
        UnName,
        UnPackage,
        UnPackageItem,
    )

# ===================================================================== #
#  State frame
# ===================================================================== #


class UnStateFrame:
    """Represents the state frame data present when HasStack flag is set."""

    def __init__(self) -> None:
        """Initialize an empty state frame with all fields set to zero."""
        self.node: int = 0
        self.state_node: int = 0
        self.offset: int = 0
        self.probe_mask: int = 0
        self.latent_action: int = 0

    def parse(self, reader: BinaryIO) -> None:
        """Read state frame fields from a binary stream.

        Args:
            reader (BinaryIO): Binary stream positioned at the state frame data.
        """
        self.node = read_index(reader)
        self.state_node = read_index(reader)
        self.probe_mask = read_ulong(reader)
        self.latent_action = read_int(reader)
        if self.node != 0:
            self.offset = read_index(reader)

    def serialize(self, writer: BinaryIO) -> None:
        """Write state frame fields to a binary stream.

        Args:
            writer (BinaryIO): Binary stream to write the state frame data to.
        """
        write_index(writer, self.node)
        write_index(writer, self.state_node)
        write_ulong(writer, self.probe_mask)
        write_int(writer, self.latent_action)
        if self.node != 0:
            write_index(writer, self.offset)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation of this state frame.

        Returns:
            Dict[str, Any]: Mapping of state frame fields, with the probe mask
                rendered as a string.
        """
        return {
            "node": self.node,
            "state_node": self.state_node,
            "offset": self.offset,
            "probe_mask": probe_mask_to_string(self.probe_mask),
            "latent_action": self.latent_action,
        }

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate this state frame from a dict representation.

        Args:
            data (Dict[str, Any]): Mapping of state frame fields, where the
                probe mask may be given as a string or an integer.
        """
        self.node = int(data.get("node", 0))
        self.state_node = int(data.get("state_node", 0))
        self.offset = int(data.get("offset", 0))
        pm = data.get("probe_mask", "")
        if isinstance(pm, str) and not pm.isdigit():
            self.probe_mask = string_to_probe_mask(pm)
        else:
            self.probe_mask = int(pm) if pm else 0
        self.latent_action = int(data.get("latent_action", 0))


# ===================================================================== #
#  Tagged property
# ===================================================================== #


class UnPropertyTag:
    """Represents a tagged property in the Unreal serialization format.

    Each tag encodes a property name, type, size, optional struct name,
    array index, and the raw property value data.  The ``info`` byte packs
    the type (bits 0-3), size encoding (bits 4-6), and an array/bool flag
    (bit 7).
    """

    def __init__(self) -> None:
        """Initialize an empty property tag with all fields set to defaults."""
        self.name_index: int = 0
        self.tag_name: Optional["UnName"] = None  # resolved from name_index
        self.info: int = 0
        self.type: int = 0
        self.struct_name_index: int = 0
        self.struct_name_entry: Optional["UnName"] = (
            None  # resolved from struct_name_index
        )
        self.size: int = 0
        self.array_index: int = 0
        self.property_data: bytes = b""
        # A struct value whose declared ``size`` stops one index short of its own
        # terminating ``None`` (ucc emits this: the size is measured by a
        # counting archive that can disagree with the real write by one compact
        # index). The engine reads a struct value as an unbounded tagged stream,
        # so that byte IS part of the value and has to be written back — see
        # UnClass._parse.
        self.trailing_none_name: Optional["UnName"] = None
        # Lazily-populated cache of object references embedded in the tag's
        # data (see _walk_tag_object_refs): the resolved items and the raw
        # ref ints they were resolved from, in traversal order.
        self._obj_ref_items: Optional[List[Optional["UnPackageItem"]]] = None
        self._obj_ref_olds: Optional[List[int]] = None

    def parse(self, reader: BinaryIO, package: Optional["UnPackage"] = None) -> None:
        """Read tag fields (after name has been read) and property data.

        Args:
            reader (BinaryIO): Binary stream positioned after the tag name.
            package (Optional["UnPackage"]): Package used to resolve the struct
                name entry from its index. Defaults to None.
        """
        self.info = read_byte(reader)
        self.type = self.info & 0x0F

        # Struct name (name table compact index) when type is StructProperty
        if self.type == UnNameMap.StructProperty:
            self.struct_name_index = read_index(reader)
            if package and 0 <= self.struct_name_index < len(package.names):
                self.struct_name_entry = package.names[self.struct_name_index]

        # Decode size from info bits 4-6
        size_type = self.info & 0x70
        if size_type == 0x00:
            self.size = 1
        elif size_type == 0x10:
            self.size = 2
        elif size_type == 0x20:
            self.size = 4
        elif size_type == 0x30:
            self.size = 12
        elif size_type == 0x40:
            self.size = 16
        elif size_type == 0x50:
            self.size = read_byte(reader)
        elif size_type == 0x60:
            self.size = read_word(reader)
        elif size_type == 0x70:
            self.size = read_int(reader)

        # Decode array index
        if (self.info & 0x80) != 0 and self.type != UnNameMap.BoolProperty:
            b = read_byte(reader)
            if (b & 0x80) == 0:
                self.array_index = b
            elif (b & 0xC0) == 0x80:
                c = read_byte(reader)
                self.array_index = ((b & 0x7F) << 8) + c
            else:
                c = read_byte(reader)
                d = read_byte(reader)
                e = read_byte(reader)
                self.array_index = ((b & 0x3F) << 24) + (c << 16) + (d << 8) + e
        else:
            self.array_index = 0

        # Read raw property value data
        self.property_data = reader.read(self.size)

    def value_data(self, package: Optional["UnPackage"] = None) -> bytes:
        """Return the value bytes as the engine reads them.

        That is ``property_data`` plus the terminating ``None`` index the
        declared size left out, when there is one — see
        :attr:`trailing_none_name`. Anything walking a struct value as a tagged
        stream has to use this, or the stream reads as truncated.

        Args:
            package (Optional[UnPackage]): Package used to resolve the
                terminator's current name index. Defaults to None (index 0).

        Returns:
            bytes: The value's full serialized form.
        """
        if self.trailing_none_name is None:
            return self.property_data
        idx = package.name_index(self.trailing_none_name) if package else 0
        out = io.BytesIO()
        write_index(out, idx)
        return self.property_data + out.getvalue()

    def serialize(
        self, writer: BinaryIO, package: Optional["UnPackage"] = None
    ) -> None:
        """Write tag fields (after name) and property data.

        Args:
            writer (BinaryIO): Binary stream to write the tag data to.
            package (Optional["UnPackage"]): Package used to re-link name
                indices from resolved name pointers. Defaults to None.
        """
        # Re-link name indices from pointers if package available
        if package is not None:
            if self.tag_name is not None:
                self.name_index = package.name_index(self.tag_name)
            if self.struct_name_entry is not None:
                self.struct_name_index = package.name_index(self.struct_name_entry)

        write_byte(writer, self.info)

        if self.type == UnNameMap.StructProperty:
            write_index(writer, self.struct_name_index)

        # Write explicit size when needed
        size_type = self.info & 0x70
        if size_type == 0x50:
            write_byte(writer, self.size)
        elif size_type == 0x60:
            write_word(writer, self.size)
        elif size_type == 0x70:
            write_int(writer, self.size)

        # Write array index
        if (self.info & 0x80) != 0 and self.type != UnNameMap.BoolProperty:
            if self.array_index <= 127:
                write_byte(writer, self.array_index)
            elif self.array_index <= 16383:
                write_byte(writer, (self.array_index >> 8) + 0x80)
                write_byte(writer, self.array_index & 0xFF)
            else:
                write_byte(writer, (self.array_index >> 24) + 0xC0)
                write_byte(writer, (self.array_index >> 16) & 0xFF)
                write_byte(writer, (self.array_index >> 8) & 0xFF)
                write_byte(writer, self.array_index & 0xFF)

        # Write raw property value data
        writer.write(self.property_data)
        # ...and the terminating 'None' the declared size left out, if any.
        if self.trailing_none_name is not None:
            idx = (
                package.name_index(self.trailing_none_name)
                if package is not None
                else 0
            )
            write_index(writer, idx)

    @staticmethod
    def _encode_size_bits(size: int) -> int:
        """Encode the size into info bits 4-6 per Unreal tagged property format.

        Args:
            size (int): Property data size in bytes.

        Returns:
            int: The encoded size bits to be OR'd into the info byte.
        """
        if size == 1:
            return 0x00
        elif size == 2:
            return 0x10
        elif size == 4:
            return 0x20
        elif size == 12:
            return 0x30
        elif size == 16:
            return 0x40
        elif size <= 255:
            return 0x50
        elif size <= 65536:
            return 0x60
        else:
            return 0x70

    @staticmethod
    def _type_to_name(type_int: int) -> str:
        """Convert a property type integer (1-15) to its UnNameMap name string.

        Args:
            type_int (int): Property type integer value.

        Returns:
            str: The UnNameMap name, or the integer as a string if unknown.
        """
        try:
            return UnNameMap(type_int).name
        except ValueError:
            return str(type_int)

    @staticmethod
    def _name_to_type(type_str: str) -> int:
        """Convert a property type name string back to its integer value.

        Args:
            type_str (str): Property type name, or an integer as a string.

        Returns:
            int: The corresponding property type integer value.

        Raises:
            ValueError: If the name is neither a known type nor an integer.
        """
        # Try looking up in UnNameMap first
        try:
            return UnNameMap[type_str].value
        except KeyError:
            pass
        # Fall back to integer parsing
        try:
            return int(type_str)
        except (ValueError, TypeError):
            raise ValueError(f"Unknown property type name: {type_str!r}")

    def _data_to_value(self, package: Optional["UnPackage"] = None) -> str:
        """Decode property_data using the codec from the property field class.

        Args:
            package (Optional["UnPackage"]): Package used by the codec to
                resolve references. Defaults to None.

        Returns:
            str: The decoded value as a string.

        Raises:
            RuntimeError: If no codec is registered for this property type.
        """
        codec = _PROPERTY_TYPE_MAP.get(self.type)
        if codec is not None:
            return codec.data_to_value(self.property_data, package)
        raise RuntimeError(
            f"No codec for property type {self._type_to_name(self.type)} "
            f"(tag '{package.names[self.name_index].name if package and 0 <= self.name_index < len(package.names) else self.name_index}')"
        )

    def _value_to_data(
        self, value: str, package: Optional["UnPackage"] = None
    ) -> bytes:
        """Encode a value string using the codec from the property field class.

        Args:
            value (str): The value string to encode.
            package (Optional["UnPackage"]): Package used by the codec to
                resolve references. Defaults to None.

        Returns:
            bytes: The encoded property data.
        """
        codec = _PROPERTY_TYPE_MAP.get(self.type)
        if codec is not None:
            return codec.value_to_data(value, package)
        return hex_to_bytes(value) if value else b""

    def to_dict(
        self, package: "UnPackage", parent_struct_name: str = ""
    ) -> Dict[str, Any]:
        """Return a dict representation of this property tag.

        The ``info`` byte and ``size`` are omitted; they are reconstructed
        from ``type``, ``array_index``, ``bool_value``, and data length
        during ``from_dict()``.  The data is stored as the element text
        under the ``_text`` key.

        ``parent_struct_name`` (when non-empty) scopes ArrayProperty inner-type
        lookup to the children of the named struct definition; this is
        necessary when this tag is itself a member of a struct (otherwise
        a global search may pick up an unrelated property with the same
        name from another class/struct).

        Args:
            package ("UnPackage"): Package used to resolve names and references.
            parent_struct_name (str): Name of the enclosing struct, used to
                scope ArrayProperty inner-type lookup. Defaults to "".

        Returns:
            Dict[str, Any]: Mapping describing this property tag.
        """
        # Use UnName pointer when available; fall back to raw name_index
        # only for legacy code paths where the pointer wasn't set.
        tag_name_idx = (
            package.name_index(self.tag_name)
            if self.tag_name is not None
            else self.name_index
        )
        d: Dict[str, Any] = {
            "name": package.resolve_name_index(tag_name_idx),
            "type": self._type_to_name(self.type),
            "array_index": self.array_index,
        }
        if self.trailing_none_name is not None:
            # The value's terminating 'None' that the declared size left out.
            d["trailing_none"] = True
        if self.type == UnNameMap.BoolProperty:
            d["bool_value"] = bool(self.info & 0x80)
        elif self.type == UnNameMap.StructProperty:
            # Resolve struct name to full qualified reference path
            struct_name_idx = (
                package.name_index(self.struct_name_entry)
                if self.struct_name_entry is not None
                else self.struct_name_index
            )
            struct_base_name = (
                package.names[struct_name_idx].name
                if 0 <= struct_name_idx < len(package.names)
                else ""
            )
            struct_ref = struct_base_name  # fallback to base name
            # Search imports then exports for matching object_name
            for imp in package.imports:
                if imp.object_name.name == struct_base_name:
                    struct_ref = package.resolve_item_ref(package.item_index(imp))
                    break
            else:
                for exp in package.exports:
                    if exp.object_name.name == struct_base_name:
                        struct_ref = package.resolve_item_ref(package.item_index(exp))
                        break
            d["struct_name"] = struct_ref
            # Decode struct fields (no hex fallback — deps loaded on import)
            struct_tags = _decode_struct_data(self.property_data, struct_ref, package)
            if struct_tags is not None:
                d["_struct_fields"] = struct_tags
            else:
                d["_text"] = bytes_to_hex(self.property_data)
        elif self.type == UnNameMap.ArrayProperty:
            count = UnArrayProperty.extract_count(self.property_data)
            d["count"] = count
            tag_name = (
                self.tag_name.name
                if self.tag_name is not None
                else (
                    package.names[self.name_index].name
                    if 0 <= self.name_index < len(package.names)
                    else ""
                )
            )
            inner_type, struct_ref = _find_array_inner_info(
                tag_name, package, parent_struct_name=parent_struct_name
            )
            if inner_type and count > 0:
                buf = io.BytesIO(self.property_data)
                read_index(buf)  # skip count
                elem_data = buf.read()
                d["inner_type"] = inner_type.split(".")[-1]
                if struct_ref:
                    d["struct_ref"] = struct_ref
                elements = _decode_array_elements(
                    elem_data, inner_type, package, struct_ref=struct_ref
                )
                if elements is not None:
                    d["_elements"] = elements
                else:
                    d["_text"] = bytes_to_hex(elem_data) if elem_data else ""
            elif count == 0:
                d["inner_type"] = inner_type.split(".")[-1] if inner_type else ""
                if struct_ref:
                    d["struct_ref"] = struct_ref
                d["_elements"] = []
            else:
                d["_text"] = self._data_to_value(package) if self.property_data else ""
        else:
            d["_text"] = self._data_to_value(package)
        return d

    def from_dict(
        self, data: Dict[str, Any], package: "UnPackage", parent_struct_name: str = ""
    ) -> None:
        """Populate this property tag from a dict representation.

        Reconstructs the ``info`` byte from ``type``, ``array_index``,
        ``bool_value``, and data.  ``size`` is inferred from data length.

        ``parent_struct_name`` (when non-empty) scopes ArrayProperty inner-type
        lookup to the children of the named struct definition; this is
        necessary when this tag is itself a member of a struct (otherwise
        a global search may pick up an unrelated property with the same
        name from another class/struct).

        Args:
            data (Dict[str, Any]): Mapping describing the property tag.
            package ("UnPackage"): Package used to link names and references.
            parent_struct_name (str): Name of the enclosing struct, used to
                scope ArrayProperty inner-type lookup. Defaults to "".
        """
        self.name_index = package.link_name_index(data.get("name", ""))
        if 0 <= self.name_index < len(package.names):
            self.tag_name = package.names[self.name_index]

        type_raw = data.get("type", "0")
        if isinstance(type_raw, str) and not type_raw.isdigit():
            self.type = self._name_to_type(type_raw)
        else:
            self.type = int(type_raw)

        self.array_index = int(data.get("array_index", 0))

        trailing = data.get("trailing_none", False)
        if isinstance(trailing, str):
            trailing = trailing.lower() in ("true", "1")
        self.trailing_none_name = package.find_name("None") if trailing else None

        # Decode data from struct fields, value text, or legacy hex "data" field
        struct_fields = data.get("_struct_fields")
        value_text = data.get("_text", data.get("data", ""))
        if self.type == UnNameMap.StructProperty and struct_fields is not None:
            struct_ref = data.get("struct_name", "")
            encoded = _encode_struct_data(struct_fields, struct_ref, package)
            if encoded is not None:
                self.property_data = encoded
            else:
                # Fallback to hex text
                self.property_data = hex_to_bytes(value_text) if value_text else b""
        elif self.type == UnNameMap.StructProperty:
            self.property_data = hex_to_bytes(value_text) if value_text else b""
        elif self.type == UnNameMap.ArrayProperty:
            count = int(data.get("count", 0))
            elements = data.get("_elements")
            inner_type_short = data.get("inner_type", "")
            if elements is not None and inner_type_short:
                inner_type = "Core." + inner_type_short
                struct_ref_enc = data.get("struct_ref", "")
                encoded_elems = _encode_array_elements(
                    elements, inner_type, package, struct_ref=struct_ref_enc
                )
                if encoded_elems is not None:
                    buf = io.BytesIO()
                    write_index(buf, count)
                    buf.write(encoded_elems)
                    self.property_data = buf.getvalue()
                else:
                    self.property_data = UnArrayProperty.value_to_data(
                        value_text, package, count=count
                    )
            else:
                self.property_data = UnArrayProperty.value_to_data(
                    value_text, package, count=count
                )
        else:
            self.property_data = self._value_to_data(value_text, package)
        self.size = len(self.property_data)

        # Reconstruct info byte
        self.info = (self.type & 0x0F) | self._encode_size_bits(self.size)
        if self.type == UnNameMap.BoolProperty:
            # For BoolProperty, bit 7 encodes the boolean value
            self.size = 0  # BoolProperty has size 0
            self.info = (self.type & 0x0F) | self._encode_size_bits(0)
            bool_val = data.get("bool_value", False)
            if isinstance(bool_val, str):
                bool_val = bool_val.lower() in ("true", "1")
            if bool_val:
                self.info |= 0x80
        else:
            # For all other types, bit 7 encodes array_index presence
            if self.array_index != 0:
                self.info |= 0x80

        if self.type == UnNameMap.StructProperty:
            struct_ref = data.get("struct_name", "")
            # Try to resolve as item reference to get the name index
            item = None
            if struct_ref:
                item_idx = package.link_item_ref(struct_ref)
                if item_idx != 0:
                    item = resolve_item(package, item_idx)
            if item is not None:
                self.struct_name_entry = item.object_name
                self.struct_name_index = package.name_index(item.object_name)
            else:
                # Fallback to direct name lookup
                self.struct_name_index = package.link_name_index(struct_ref)
                if 0 <= self.struct_name_index < len(package.names):
                    self.struct_name_entry = package.names[self.struct_name_index]


# ===================================================================== #
#  Object hierarchy
# ===================================================================== #


class UnObject(ABC):
    """Abstract base class for objects stored in an Unreal package export."""

    def __init__(self, export: "UnExport") -> None:
        """Initialize the object bound to its owning export.

        Args:
            export ("UnExport"): The package export that owns this object.
        """
        self.export = export
        self.state_frame: Optional[UnStateFrame] = None
        self.tagged_properties: List[UnPropertyTag] = []
        self._none_name: Optional["UnName"] = None  # resolved from _none_name_index

    def _skip_tagged_properties(self) -> bool:
        """Return True to skip object-level tagged properties.

        Class objects override this because class serialization skips
        tagged properties for the class definition itself.

        Returns:
            bool: True when tagged properties should be skipped.
        """
        return False

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the state frame and tagged properties from a binary stream.

        Tagged properties are read until the "None" name terminator is reached.

        Args:
            reader (BinaryIO): Binary stream positioned at the object data.
        """
        if (self.export.flags & UnObjectFlags.HasStack) != 0:
            self.state_frame = UnStateFrame()
            self.state_frame.parse(reader)
        else:
            self.state_frame = UnStateFrame()

        if self._skip_tagged_properties():
            self.tagged_properties = []
            self._none_name_index = 0
            return

        # Read tagged properties until the None terminator
        self.tagged_properties = []
        self._none_name = None
        pkg = self.export.package
        while True:
            name_index = read_index(reader)
            # Check if name resolves to "None" (the terminator)
            if (
                0 <= name_index < len(pkg.names)
                and pkg.names[name_index].name == "None"
            ):
                self._none_name = pkg.names[name_index]
                break

            tag = UnPropertyTag()
            tag.name_index = name_index
            if 0 <= name_index < len(pkg.names):
                tag.tag_name = pkg.names[name_index]
            tag.parse(reader, package=pkg)
            self.tagged_properties.append(tag)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the state frame and tagged properties to a binary stream.

        Writes each tagged property followed by the "None" name terminator.

        Args:
            writer (BinaryIO): Binary stream to write the object data to.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        if (self.export.flags & UnObjectFlags.HasStack) != 0:
            if self.state_frame is None:
                self.state_frame = UnStateFrame()
            self.state_frame.serialize(writer)

        if self._skip_tagged_properties():
            return

        # Write tagged properties followed by the None terminator
        pkg = self.export.package
        for tag in self.tagged_properties:
            if tag.tag_name is not None:
                tag.name_index = pkg.name_index(tag.tag_name)
            write_index(writer, tag.name_index)
            tag.serialize(writer, package=pkg)
        # Write the None terminator
        none_idx = pkg.name_index(self._none_name) if self._none_name else 0
        write_index(writer, none_idx)

    def resolve(self) -> None:
        """Resolve integer references into item pointers."""
        for tag in self.tagged_properties:
            _resolve_tag_object_refs(tag, self.export.package)

    def link(self) -> None:
        """Link item pointers back into integer references."""
        for tag in self.tagged_properties:
            _link_tag_object_refs(tag, self.export.package)

    def clear_resolved(self) -> None:
        """Clear resolved item pointers."""
        pass

    def clear_links(self) -> None:
        """Zero out integer reference properties, leaving item pointers intact."""
        pass

    def drop_generations(self) -> None:
        """Clean up this object for generation-free serialisation."""
        self._none_name = self.export.package.find_name("None")

    def remap_name_indices(self, index_map: Dict[int, int]) -> None:
        """Remap raw name-table indices embedded in tagged property data.

        Called after ``deduplicate_names()`` rebuilds the name table.

        Args:
            index_map (Dict[int, int]): Mapping of old name index to new name
                index.
        """
        pkg = self.export.package
        for tag in self.tagged_properties:
            _remap_property_tag_data(tag, index_map, pkg)

    def deduplicate_names(self) -> None:
        """Re-resolve UnName pointers after the name table has been rebuilt.

        Each pointer is re-resolved via ``pkg.find_name(name.name)`` so it
        points to the canonical (first) entry in the deduplicated table.
        """
        pkg = self.export.package

        def _resolve(n):
            """Return the canonical UnName pointer for a given name entry.

            Args:
                n (Optional["UnName"]): The name entry to re-resolve, or None.

            Returns:
                Optional["UnName"]: The canonical name pointer, or None when
                    *n* is None.
            """
            return pkg.find_name(n.name) if n else None

        self._none_name = _resolve(self._none_name)
        for tag in self.tagged_properties:
            tag.tag_name = _resolve(tag.tag_name)
            tag.struct_name_entry = _resolve(tag.struct_name_entry)

    def export_xml(self, obj_dict: Dict[str, Any], output_dir: str) -> None:
        """Post-process *obj_dict* during XML export (e.g. write sidecar files).

        Subclasses may override to extract large data into external files.

        Args:
            obj_dict (Dict[str, Any]): The object's dict representation to
                post-process in place.
            output_dir (str): Directory where sidecar files may be written.
        """
        pass

    def import_xml(self, obj_dict: Dict[str, Any], input_dir: str) -> None:
        """Pre-process *obj_dict* during XML import (e.g. read sidecar files).

        Subclasses may override to load external data back into the dict.

        Args:
            obj_dict (Dict[str, Any]): The object's dict representation to
                pre-process in place.
            input_dir (str): Directory from which sidecar files may be read.
        """
        pass

    def parse(self) -> None:
        """Parse this object from its export's raw binary data, if present."""
        if self.export.export_data is None:
            return
        mem = io.BytesIO(self.export.export_data)
        self._parse(mem)
        mem.close()

    def serialize(self, stream_position: int) -> None:
        """Serialize this object back into its export's raw binary data.

        Does nothing when the export has no data.

        Args:
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        if self.export.export_data is None:
            return
        mem = io.BytesIO()
        self._serialize(mem, stream_position)
        self.export.export_data = mem.getvalue()
        mem.close()

    def _resolve_object_ref(self, ref: int) -> str:
        """Resolve a compact object reference index to a prefixed name string.

        Args:
            ref (int): Compact object reference index.

        Returns:
            str: The resolved prefixed name string.
        """
        return self.export.package.resolve_item_ref(ref)

    def _link_object_ref(self, name: str) -> int:
        """Resolve a prefixed name string back to a compact reference index.

        Args:
            name (str): Prefixed name string.

        Returns:
            int: The compact object reference index.
        """
        return self.export.package.link_item_ref(name)

    def _resolve_name_index(self, idx: int) -> str:
        """Resolve a name table index to a ``Name`` or ``Name@N`` string.

        Args:
            idx (int): Name table index.

        Returns:
            str: The resolved ``Name`` or ``Name@N`` string.
        """
        return self.export.package.resolve_name_index(idx)

    def _link_name_index(self, name: str) -> int:
        """Resolve a ``Name`` or ``Name@N`` string back to a name table index.

        Args:
            name (str): A ``Name`` or ``Name@N`` string.

        Returns:
            int: The name table index.
        """
        return self.export.package.link_name_index(name)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation of this object's parsed data.

        Returns:
            Dict[str, Any]: The object's serializable dict representation.
        """
        d: Dict[str, Any] = {
            "type": self.__class__.__name__,
        }
        if self.tagged_properties:
            # Scope ArrayProperty inner-type lookup to the export's own class so
            # a property name shared by several classes (``Materials`` is both
            # an object array on a LOD mesh and a struct array on a static mesh)
            # resolves against the class that actually declares this value.
            d["tagged_properties"] = [
                t.to_dict(
                    self.export.package,
                    parent_struct_name=self.export.class_name_string,
                )
                for t in self.tagged_properties
            ]
        # Only save state_frame if it differs from default (all zeros)
        if self.state_frame is not None:
            sf_dict = self.state_frame.to_dict()
            if any(v != 0 for v in sf_dict.values()):
                d["state_frame"] = sf_dict
        # Save none_index only when needed for disambiguation (duplicate "None" entries)
        none_name = getattr(self, "_none_name", None)
        if none_name is not None:
            pkg = self.export.package
            resolved = pkg.resolve_name_index(pkg.name_index(none_name))
            if resolved != "None":
                d["none_index"] = resolved
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate this object from a dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        self.tagged_properties = []
        for tag_data in data.get("tagged_properties", []):
            tag = UnPropertyTag()
            tag.from_dict(tag_data, self.export.package)
            self.tagged_properties.append(tag)
        sf_data = data.get("state_frame")
        if sf_data is not None and isinstance(sf_data, dict):
            self.state_frame = UnStateFrame()
            self.state_frame.from_dict(sf_data)
        else:
            self.state_frame = None
        # Restore none_name_index from Name@N string or raw integer
        none_val = data.get("none_index")
        if none_val is not None:
            idx = self._link_name_index(str(none_val))
            pkg = self.export.package
            self._none_name = pkg.names[idx] if 0 <= idx < len(pkg.names) else None
        else:
            self._none_name = self.export.package.find_name("None")

    @abstractmethod
    def dump(self) -> str:
        """Return a human-readable summary string for this object.

        Returns:
            str: A one-line description of the object.
        """
        ...


class UnField(UnObject):
    """Base class for fields (structs, enums, consts, etc.)."""

    def __init__(self, export: "UnExport") -> None:
        """Initialize the field with empty super and next references.

        Args:
            export ("UnExport"): The export entry that owns this field.
        """
        super().__init__(export)
        self.super_index: int = 0
        self.next_reference: int = 0
        self.super_item: Optional["UnPackageItem"] = None
        self.next_item: Optional["UnPackageItem"] = None

    def resolve(self) -> None:
        """Resolve the super and next references to package items."""
        super().resolve()
        self.super_item = resolve_item(self.export.package, self.super_index)
        self.next_item = resolve_item(self.export.package, self.next_reference)

    def link(self) -> None:
        """Link the resolved super and next items back to indices."""
        super().link()
        self.super_index = link_item(self.export.package, self.super_item)
        self.next_reference = link_item(self.export.package, self.next_item)

    def clear_resolved(self) -> None:
        """Clear the resolved super and next items."""
        self.super_item = None
        self.next_item = None

    def clear_links(self) -> None:
        """Clear the super and next reference indices."""
        self.super_index = 0
        self.next_reference = 0

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the field's super and next references from the stream.

        Args:
            reader (BinaryIO): The binary stream to read from.
        """
        super()._parse(reader)
        self.super_index = read_index(reader)
        self.next_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the field's super and next references to the stream.

        Args:
            writer (BinaryIO): The binary stream to write to.
            stream_position (int): The current position within the stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.super_index)
        write_index(writer, self.next_reference)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the field to a dictionary with resolved references.

        Returns:
            Dict[str, Any]: The dictionary representation of the field.
        """
        d = super().to_dict()
        d["super"] = self._resolve_object_ref(self.super_index)
        d["next"] = self._resolve_object_ref(self.next_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the field from a dictionary, linking references.

        Args:
            data (Dict[str, Any]): The dictionary to load field data from.
        """
        super().from_dict(data)
        self.super_index = self._link_object_ref(data.get("super", ""))
        self.next_reference = self._link_object_ref(data.get("next", ""))


class UnStruct(UnField):
    """Represents a struct definition in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialize the struct with default children, flags, and token state.

        Args:
            export ("UnExport"): The export entry that owns this struct.
        """
        super().__init__(export)
        self.script_text_reference: int = 0
        self.children_reference: int = 0
        self.script_text: Optional["UnPackageItem"] = None
        self.children: Optional["UnPackageItem"] = None
        self.friendly_name: Optional["UnName"] = None
        self.struct_flags: int = 0
        self.lines: int = 0
        self.text_position: int = 0
        self.script_size: int = 0
        self.token_parser: Optional["TokenStreamParser"] = None

    # ------------------------------------------------------------------ #
    #  Binary (native) struct serialisation
    # ------------------------------------------------------------------ #

    def _iter_serializable_properties(self) -> "List[UnProperty]":
        """Collect the child properties that take part in binary serialisation.

        Walks the struct's children chain and yields ``UnProperty`` objects. A
        property is skipped if its ``PropertyFlags`` include ``Native`` or
        ``Transient`` (the archive is always persistent). ``Deprecated``
        properties are included on load but could be skipped on save; they are
        included here for roundtrip fidelity.

        Returns:
            "List[UnProperty]": The properties to serialise, in chain order.
        """
        props: List["UnProperty"] = []
        child = self.children
        while child is not None:
            obj = child.object
            if obj is not None and isinstance(obj, UnProperty):
                pf = obj.property_flags
                skip = (pf & int(UnPropertyFlags.Native)) != 0 or (
                    pf & int(UnPropertyFlags.Transient)
                ) != 0
                if not skip:
                    props.append(obj)
            if isinstance(obj, UnField):
                child = obj.next_item
            else:
                break
        return props

    def parse_bin(self, reader: BinaryIO, max_read_bytes: int = 0) -> bytes:
        """Read native struct data via per-property ``parse_item`` calls.

        Iterates over the struct's property children (via
        ``_iter_serializable_properties``). For each property, calls
        ``parse_item`` for each of its ``ArrayDim`` elements.

        Args:
            reader (BinaryIO): The binary stream to read from.
            max_read_bytes (int): Maximum number of bytes to read. Defaults to 0.

        Returns:
            bytes: The concatenated binary data for all properties.
        """
        buf = io.BytesIO()
        for prop in self._iter_serializable_properties():
            for idx in range(prop.array_dim):
                elem = prop.parse_item(reader, 0)
                buf.write(elem)
        return buf.getvalue()

    def serialize_bin(self, writer: BinaryIO, data: bytes) -> None:
        """Write native struct data via per-property ``serialize_item`` calls.

        Iterates over the struct's property children (via
        ``_iter_serializable_properties``). For each property, calls
        ``serialize_item`` for each of its ``ArrayDim`` elements.

        Args:
            writer (BinaryIO): The binary stream to write to.
            data (bytes): The source binary data to re-emit.
        """
        buf = io.BytesIO(data)
        for prop in self._iter_serializable_properties():
            for idx in range(prop.array_dim):
                # Read each element from the source buffer via parse_item,
                # then re-emit it through serialize_item.
                elem = prop.parse_item(buf, 0)
                prop.serialize_item(writer, elem)

    def _parse(self, reader: BinaryIO) -> None:
        """Parse struct fields and the token stream from the stream.

        Args:
            reader (BinaryIO): The binary stream to read from.
        """
        super()._parse(reader)
        self.script_text_reference = read_index(reader)
        self.children_reference = read_index(reader)
        name_index = read_index(reader)
        self.friendly_name = self.export.package.names[name_index]
        self.struct_flags = read_uint(reader)
        self.lines = read_int(reader)
        self.text_position = read_int(reader)
        self.script_size = read_int(reader)

        # Parse the token stream
        if self.script_size > 0:
            self.token_parser = TokenStreamParser(self.export.package)
            self.token_parser.parse_stream(reader, self.script_size)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize struct fields and the token stream to the stream.

        Args:
            writer (BinaryIO): The binary stream to write to.
            stream_position (int): The current position within the stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.script_text_reference)
        write_index(writer, self.children_reference)
        assert self.friendly_name is not None, "struct friendly_name must be set"
        name_index = self.export.package.name_index(self.friendly_name)
        write_index(writer, name_index)
        write_uint(writer, self.struct_flags)
        write_int(writer, self.lines)
        write_int(writer, self.text_position)

        if self.token_parser is not None and self.token_parser.tokens:
            # Write script_size placeholder, serialize tokens, then fix up
            size_pos = writer.tell()
            write_int(writer, 0)  # placeholder
            script_size = self.token_parser.serialize_stream(writer)
            end_pos = writer.tell()
            writer.seek(size_pos)
            write_int(writer, script_size)
            writer.seek(end_pos)
        else:
            write_int(writer, 0)

    def resolve(self) -> None:
        """Resolve the script text and children references to package items."""
        super().resolve()
        self.script_text = resolve_item(self.export.package, self.script_text_reference)
        self.children = resolve_item(self.export.package, self.children_reference)
        if self.token_parser is not None:
            self.token_parser.resolve_objects()

    def link(self) -> None:
        """Link the resolved script text and children items back to indices."""
        super().link()
        self.script_text_reference = link_item(self.export.package, self.script_text)
        self.children_reference = link_item(self.export.package, self.children)
        if self.token_parser is not None:
            self.token_parser.link_objects()

    def clear_resolved(self) -> None:
        """Clear the resolved script text and children items."""
        super().clear_resolved()
        self.script_text = None
        self.children = None

    def clear_links(self) -> None:
        """Clear the script text and children reference indices."""
        super().clear_links()
        self.script_text_reference = 0
        self.children_reference = 0

    def remap_name_indices(self, index_map: Dict[int, int]) -> None:
        """Remap name indices on the struct and its token stream.

        Args:
            index_map (Dict[int, int]): Mapping from old to new name indices.
        """
        super().remap_name_indices(index_map)
        if self.token_parser is not None:
            self.token_parser.remap_name_indices(index_map)

    def deduplicate_names(self):
        """Repoint the friendly name to the deduplicated package name entry."""
        super().deduplicate_names()
        pkg = self.export.package
        if self.friendly_name is not None:
            self.friendly_name = pkg.find_name(self.friendly_name.name)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the struct, including flags and tokens, to a dictionary.

        Returns:
            Dict[str, Any]: The dictionary representation of the struct.
        """
        d = super().to_dict()
        d["script_text"] = self._resolve_object_ref(self.script_text_reference)
        d["children"] = self._resolve_object_ref(self.children_reference)
        d["friendly_name"] = self.friendly_name.name if self.friendly_name else ""
        d["struct_flags"] = struct_flags_to_string(UnStructFlags(self.struct_flags))
        d["lines"] = self.lines
        d["text_position"] = self.text_position

        if self.token_parser is not None and self.token_parser.tokens:
            d["tokens"] = self.token_parser.tokens_to_dict_list()
        else:
            d["tokens"] = []
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the struct from a dictionary, linking references and tokens.

        Args:
            data (Dict[str, Any]): The dictionary to load struct data from.

        Raises:
            RuntimeError: If the friendly name is not found in the package name
                table.
        """
        super().from_dict(data)
        self.script_text_reference = self._link_object_ref(
            data.get("script_text", data.get("script_text_ref", ""))
        )
        self.children_reference = self._link_object_ref(
            data.get("children", data.get("children_ref", ""))
        )

        fname = data.get("friendly_name", "")
        if fname:
            self.friendly_name = self.export.package.find_name(fname)
            if self.friendly_name is None:
                raise RuntimeError(f"Name '{fname}' not found in package name table.")
        else:
            self.friendly_name = None

        sf = data.get("struct_flags", "")
        if isinstance(sf, str) and sf and not sf.isdigit():
            self.struct_flags = int(string_to_struct_flags(sf))
        else:
            self.struct_flags = int(sf) if sf else 0
        self.lines = int(data.get("lines", 0))
        self.text_position = int(data.get("text_position", 0))

        tokens_data = data.get("tokens", [])
        if isinstance(tokens_data, list) and tokens_data:
            self.token_parser = TokenStreamParser(self.export.package)
            self.token_parser.tokens_from_dict_list(tokens_data)
            # Compute script_size from serialized tokens
            buf = io.BytesIO()
            self.script_size = self.token_parser.serialize_stream(buf)
        else:
            self.token_parser = None
            self.script_size = 0

    def export_xml(self, obj_dict: Dict[str, Any], output_dir: str) -> None:
        """Write tokens to a sidecar assembler file under ``UnToken/``.

        The token stream is rendered as a human-readable, assembler-style
        disassembly (see :mod:`ut2004packageutil.package.token_asm`) rather
        than XML, which is far easier to follow than the nested ``<Token>``
        tree.  The sidecar uses a ``.uasm`` extension.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary; its tokens are
                moved to the sidecar file and a "tokens_file" key is added.
            output_dir (str): The directory to write the sidecar file into.
        """
        tokens_data = obj_dict.pop("tokens", [])
        if not tokens_data:
            return

        tokens_name = self.export.object_name_string
        display_offset = bool(
            self.token_parser is not None and self.token_parser._emit_icode
        )
        body = tokens_to_asm(tokens_data, display_offset=display_offset)
        text = f"; {tokens_name} token disassembly\n{body}"

        tokens_subdir = os.path.join(output_dir, "UnToken")
        os.makedirs(tokens_subdir, exist_ok=True)
        tokens_path = os.path.join(tokens_subdir, tokens_name + ".uasm")
        with open(tokens_path, "w", encoding="utf-8") as f:
            f.write(text)

        obj_dict["tokens_file"] = tokens_name

    def import_xml(self, obj_dict: Dict[str, Any], input_dir: str) -> None:
        """Read tokens from a sidecar assembler file under ``UnToken/``.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary; the "tokens_file"
                key is consumed and a "tokens" list is populated.
            input_dir (str): The directory to read the sidecar file from.
        """
        tokens_name = obj_dict.pop("tokens_file", "")
        if not tokens_name:
            return

        tokens_path = os.path.join(input_dir, "UnToken", tokens_name + ".uasm")
        with open(tokens_path, "r", encoding="utf-8") as f:
            text = f.read()

        obj_dict["tokens"] = asm_to_tokens(text)

    def dump(self) -> str:
        """Return a human-readable summary of the struct.

        Returns:
            str: A single-line description of the struct.
        """
        fname = self.friendly_name.name if self.friendly_name else "?"
        token_count = len(self.token_parser.tokens) if self.token_parser else 0
        return (
            f"UnStruct: {self.export.object_name_string} "
            f"(Friendly Name: {fname}, Lines: {self.lines}, "
            f"Text Position = {self.text_position}, Script Size = {self.script_size}, "
            f"Tokens = {token_count})"
        )


class UnFunction(UnStruct):
    """Represents a function definition in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the function object.

        Args:
            export ("UnExport"): The owning package export entry.
        """
        super().__init__(export)
        self.native_index: int = 0
        self.operator_precedence: int = 0
        self.function_flags: "UnFunctionFlags" = UnFunctionFlags(0)
        self.rep_offset: int = 0

    def _parse(self, reader: BinaryIO) -> None:
        """Read function-specific fields from a binary stream.

        Args:
            reader (BinaryIO): The binary stream to read from.
        """
        super()._parse(reader)
        self.native_index = read_word(reader)
        self.operator_precedence = read_byte(reader)
        self.function_flags = UnFunctionFlags(read_uint(reader))
        if self.function_flags & UnFunctionFlags.Net:
            self.rep_offset = read_word(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Write function-specific fields to a binary stream.

        Args:
            writer (BinaryIO): The binary stream to write to.
            stream_position (int): The current absolute stream position.
        """
        super()._serialize(writer, stream_position)
        write_word(writer, self.native_index)
        write_byte(writer, self.operator_precedence)
        write_uint(writer, int(self.function_flags))
        if self.function_flags & UnFunctionFlags.Net:
            write_word(writer, self.rep_offset)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation of this function.

        Returns:
            Dict[str, Any]: The serialisable function data.
        """
        d = super().to_dict()
        d["native_index"] = self.native_index
        d["operator_precedence"] = self.operator_precedence
        d["function_flags"] = function_flags_to_string(self.function_flags)
        d["rep_offset"] = self.rep_offset
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate this function from a dict representation.

        Args:
            data (Dict[str, Any]): The dict to read function data from.
        """
        super().from_dict(data)
        self.native_index = int(data.get("native_index", 0))
        self.operator_precedence = int(data.get("operator_precedence", 0))
        ff = data.get("function_flags", "")
        if isinstance(ff, str) and ff and not ff.isdigit():
            self.function_flags = UnFunctionFlags(string_to_function_flags(ff))
        else:
            self.function_flags = UnFunctionFlags(int(ff) if ff else 0)
        self.rep_offset = int(data.get("rep_offset", 0))

    def dump(self) -> str:
        """Return a human-readable one-line summary of this function.

        Returns:
            str: The formatted summary string.
        """
        fname = self.friendly_name.name if self.friendly_name else "?"
        token_count = len(self.token_parser.tokens) if self.token_parser else 0
        return (
            f"UnFunction: {self.export.object_name_string} "
            f"(Friendly Name: {fname}, Native: {self.native_index}, "
            f"Precedence: {self.operator_precedence}, "
            f"FuncFlags: 0x{self.function_flags:08X}, "
            f"Script Size = {self.script_size}, Tokens = {token_count})"
        )


class UnState(UnStruct):
    """Represents a state definition in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the state object.

        Args:
            export ("UnExport"): The owning package export entry.
        """
        super().__init__(export)
        self.probe_mask: int = 0
        self.ignore_mask: int = 0
        self.label_table_offset: int = 0
        self.state_flags: int = 0

    def _parse(self, reader: BinaryIO) -> None:
        """Read state-specific fields from a binary stream.

        Args:
            reader (BinaryIO): The binary stream to read from.
        """
        super()._parse(reader)
        self.probe_mask = read_ulong(reader)
        self.ignore_mask = read_ulong(reader)
        self.label_table_offset = read_word(reader)
        self.state_flags = read_uint(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Write state-specific fields to a binary stream.

        Args:
            writer (BinaryIO): The binary stream to write to.
            stream_position (int): The current absolute stream position.
        """
        super()._serialize(writer, stream_position)
        write_ulong(writer, self.probe_mask)
        write_ulong(writer, self.ignore_mask)
        write_word(writer, self.label_table_offset)
        write_uint(writer, self.state_flags)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation of this state.

        Returns:
            Dict[str, Any]: The serialisable state data.
        """
        d = super().to_dict()
        d["probe_mask"] = probe_mask_to_string(self.probe_mask)
        d["ignore_mask"] = ignore_mask_to_string(self.ignore_mask)
        d["label_table_offset"] = self.label_table_offset
        d["state_flags"] = state_flags_to_string(UnStateFlags(self.state_flags))
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate this state from a dict representation.

        Args:
            data (Dict[str, Any]): The dict to read state data from.
        """
        super().from_dict(data)
        pm = data.get("probe_mask", "")
        if isinstance(pm, str) and not pm.isdigit():
            self.probe_mask = string_to_probe_mask(pm)
        else:
            self.probe_mask = int(pm) if pm else 0
        im = data.get("ignore_mask", "")
        if isinstance(im, str) and not im.isdigit():
            self.ignore_mask = string_to_ignore_mask(im)
        else:
            self.ignore_mask = int(im) if im else 0
        self.label_table_offset = int(data.get("label_table_offset", 0))
        sf = data.get("state_flags", "")
        if isinstance(sf, str) and sf and not sf.isdigit():
            self.state_flags = int(string_to_state_flags(sf))
        else:
            self.state_flags = int(sf) if sf else 0

    def dump(self) -> str:
        """Return a human-readable one-line summary of this state.

        Returns:
            str: The formatted summary string.
        """
        fname = self.friendly_name.name if self.friendly_name else "?"
        token_count = len(self.token_parser.tokens) if self.token_parser else 0
        return (
            f"UnState: {self.export.object_name_string} "
            f"(Friendly Name: {fname}, "
            f"StateFlags: 0x{self.state_flags:08X}, "
            f"Script Size = {self.script_size}, Tokens = {token_count})"
        )


class UnClass(UnState):
    """Represents a class definition in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the class definition.

        Args:
            export (UnExport): The export this class belongs to.
        """
        super().__init__(export)
        self.class_flags: int = 0
        self.class_guid: "UnGuid" = UnGuid()
        self.dependencies: List[
            Dict[str, Any]
        ] = []  # [{class_ref, deep, script_text_crc}]
        self.package_imports: List[int] = []  # name indices
        self.package_import_names: List["UnName"] = []  # resolved pointers
        self.class_within: int = 0  # compact-index object ref
        self.class_within_item: Optional["UnPackageItem"] = None  # resolved
        self.class_config_name: int = 0  # name index
        self.class_config_name_entry: Optional["UnName"] = None  # resolved
        self.hide_categories: List[int] = []  # name indices
        self.hide_category_names: List["UnName"] = []  # resolved pointers
        self.default_properties: List[UnPropertyTag] = []
        self._default_none_name: Optional["UnName"] = None  # resolved

    def _skip_tagged_properties(self) -> bool:
        """Return whether object-level tagged properties are skipped.

        Class definitions skip object-level tagged properties.

        Returns:
            bool: Always True for class definitions.
        """
        return True

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the class definition from a binary stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)

        # ClassFlags (uint32) + ClassGuid (16 bytes)
        self.class_flags = read_uint(reader)
        self.class_guid = UnGuid.from_stream(reader)

        # Dependencies: count-prefixed list of (class_ref, deep, script_text_crc)
        dep_count = read_index(reader)
        self.dependencies = []
        for _ in range(dep_count):
            class_ref = read_index(reader)  # class compact-index object ref
            deep = bool(read_int(reader))  # bool stored as int
            script_text_crc = read_uint(reader)
            self.dependencies.append(
                {
                    "class_ref": class_ref,
                    "deep": deep,
                    "script_text_crc": script_text_crc,
                }
            )

        # PackageImports: count-prefixed list of name indices
        imp_count = read_index(reader)
        pkg = self.export.package
        self.package_imports = []
        self.package_import_names = []
        for _ in range(imp_count):
            idx = read_index(reader)
            self.package_imports.append(idx)
            if 0 <= idx < len(pkg.names):
                self.package_import_names.append(pkg.names[idx])

        # ClassWithin (compact-index object ref) + ClassConfigName (name index)
        self.class_within = read_index(reader)
        self.class_config_name = read_index(reader)
        if 0 <= self.class_config_name < len(pkg.names):
            self.class_config_name_entry = pkg.names[self.class_config_name]

        # HideCategories: count-prefixed list of name indices
        hide_count = read_index(reader)
        self.hide_categories = []
        self.hide_category_names = []
        for _ in range(hide_count):
            idx = read_index(reader)
            self.hide_categories.append(idx)
            if 0 <= idx < len(pkg.names):
                self.hide_category_names.append(pkg.names[idx])

        # Class defaults: tagged properties (None-terminated)
        #
        # A struct value's declared size can stop one compact index short of the
        # value's own terminating 'None' (ucc measures the size with a counting
        # archive that disagrees with the real write by that one index). The
        # engine does not care — it reads a struct value as an unbounded tagged
        # stream and consumes the byte — but we do: read as the list terminator,
        # that byte would end the list early and the REAL terminator would be
        # dropped on write, leaving a struct value the engine then reads straight
        # past. So a 'None' that follows a struct value whose own stream is
        # unterminated belongs to that value, not to this list.
        self.default_properties = []
        self._default_none_name = None
        while True:
            name_index = read_index(reader)
            if (
                0 <= name_index < len(pkg.names)
                and pkg.names[name_index].name == "None"
            ):
                prev = self.default_properties[-1] if self.default_properties else None
                if (
                    prev is not None
                    and prev.trailing_none_name is None
                    and prev.type == UnNameMap.StructProperty
                    and prev.struct_name_entry is not None
                    # A native-serialize struct (Vector, Color, …) is positional,
                    # not tagged, so it never carries a terminator and its bytes
                    # must not be read as tags at all.
                    and not _is_native_serialize_struct(prev.struct_name_entry.name)
                    and _tagged_stream_is_unterminated(prev.property_data, pkg)
                ):
                    prev.trailing_none_name = pkg.names[name_index]
                    continue
                self._default_none_name = pkg.names[name_index]
                break
            tag = UnPropertyTag()
            tag.name_index = name_index
            if 0 <= name_index < len(pkg.names):
                tag.tag_name = pkg.names[name_index]
            tag.parse(reader, package=pkg)
            self.default_properties.append(tag)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the class definition to a binary stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): Absolute stream offset of the object data.
        """
        super()._serialize(writer, stream_position)

        # ClassFlags + ClassGuid
        write_uint(writer, self.class_flags)
        self.class_guid.write(writer)

        # Dependencies
        write_index(writer, len(self.dependencies))
        for dep in self.dependencies:
            write_index(writer, dep["class_ref"])
            write_int(writer, int(dep["deep"]))
            write_uint(writer, dep["script_text_crc"])

        pkg = self.export.package

        # Re-link name pointers to indices
        self.package_imports = [pkg.name_index(n) for n in self.package_import_names]
        if self.class_config_name_entry is not None:
            self.class_config_name = pkg.name_index(self.class_config_name_entry)
        self.hide_categories = [pkg.name_index(n) for n in self.hide_category_names]

        # PackageImports
        write_index(writer, len(self.package_imports))
        for ni in self.package_imports:
            write_index(writer, ni)

        # ClassWithin + ClassConfigName
        write_index(writer, self.class_within)
        write_index(writer, self.class_config_name)

        # HideCategories
        write_index(writer, len(self.hide_categories))
        for ni in self.hide_categories:
            write_index(writer, ni)

        # Class defaults tagged properties + None terminator
        for tag in self.default_properties:
            if tag.tag_name is not None:
                tag.name_index = pkg.name_index(tag.tag_name)
            write_index(writer, tag.name_index)
            tag.serialize(writer, package=pkg)
        none_idx = (
            pkg.name_index(self._default_none_name) if self._default_none_name else 0
        )
        write_index(writer, none_idx)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the class definition to a dictionary.

        Returns:
            Dict[str, Any]: The class data as a dictionary.
        """
        d = super().to_dict()
        d["class_flags"] = class_flags_to_string(UnClassFlags(self.class_flags))
        d["class_guid"] = self.class_guid.to_hex()

        d["dependencies"] = [
            {
                "class": self._resolve_object_ref(dep["class_ref"]),
                "deep": bool(dep["deep"]),
                "script_text_crc": dep["script_text_crc"],
            }
            for dep in self.dependencies
        ]

        pkg = self.export.package
        d["package_imports"] = [
            pkg.resolve_name_index(pkg.name_index(n)) for n in self.package_import_names
        ]

        d["class_within"] = self._resolve_object_ref(self.class_within)
        d["class_config_name"] = (
            pkg.resolve_name_index(pkg.name_index(self.class_config_name_entry))
            if self.class_config_name_entry
            else ""
        )

        d["hide_categories"] = [
            pkg.resolve_name_index(pkg.name_index(n)) for n in self.hide_category_names
        ]

        if self.default_properties:
            d["default_properties"] = [
                t.to_dict(self.export.package) for t in self.default_properties
            ]
        # Save default_none_index only when needed for disambiguation
        if self._default_none_name is not None:
            pkg = self.export.package
            resolved = pkg.resolve_name_index(pkg.name_index(self._default_none_name))
            if resolved != "None":
                d["default_none_index"] = resolved
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the class definition from a dictionary.

        Args:
            data (Dict[str, Any]): The class data to load.
        """
        super().from_dict(data)

        cf = data.get("class_flags", "")
        if isinstance(cf, str) and cf and not cf.isdigit():
            self.class_flags = int(string_to_class_flags(cf))
        else:
            self.class_flags = int(cf) if cf else 0

        guid_hex = data.get("class_guid", "")
        self.class_guid = UnGuid.from_hex(guid_hex) if guid_hex else UnGuid()

        self.dependencies = []
        for dep_data in data.get("dependencies", []):
            deep_raw = dep_data.get("deep", False)
            if isinstance(deep_raw, bool):
                deep = deep_raw
            elif isinstance(deep_raw, str):
                deep = deep_raw.lower() in ("true", "1")
            else:
                deep = bool(deep_raw)
            self.dependencies.append(
                {
                    "class_ref": self._link_object_ref(dep_data.get("class", "")),
                    "deep": deep,
                    "script_text_crc": int(dep_data.get("script_text_crc", 0)),
                }
            )

        pkg = self.export.package
        self.package_import_names = []
        for n in data.get("package_imports", []):
            idx = pkg.link_name_index(n)
            if 0 <= idx < len(pkg.names):
                self.package_import_names.append(pkg.names[idx])

        self.class_within = self._link_object_ref(data.get("class_within", ""))
        ccn = data.get("class_config_name", "")
        if ccn:
            idx = pkg.link_name_index(ccn)
            self.class_config_name_entry = (
                pkg.names[idx] if 0 <= idx < len(pkg.names) else None
            )
        else:
            self.class_config_name_entry = None

        self.hide_category_names = []
        for n in data.get("hide_categories", []):
            idx = pkg.link_name_index(n)
            if 0 <= idx < len(pkg.names):
                self.hide_category_names.append(pkg.names[idx])

        self.default_properties = []
        for tag_data in data.get("default_properties", []):
            tag = UnPropertyTag()
            tag.from_dict(tag_data, self.export.package)
            self.default_properties.append(tag)

        none_val = data.get("default_none_index")
        if none_val is not None:
            idx = self._link_name_index(str(none_val))
            self._default_none_name = (
                pkg.names[idx] if 0 <= idx < len(pkg.names) else None
            )
        else:
            self._default_none_name = pkg.find_name("None")

    def drop_generations(self) -> None:
        """Clean up the class for generation-free serialisation."""
        super().drop_generations()
        self._default_none_name = self.export.package.find_name("None")

        # Deduplicate dependencies
        seen: Dict[tuple, int] = {}
        deduped: List[Dict[str, Any]] = []
        for dep in self.dependencies:
            key = (dep["class_ref"], dep["script_text_crc"])
            if key in seen:
                deduped[seen[key]]["deep"] = True
            else:
                seen[key] = len(deduped)
                deduped.append(dep.copy())
                deduped[-1]["deep"] = True
        self.dependencies = deduped

    def resolve(self) -> None:
        """Resolve class-level object references to package item pointers.

        Covers ``class_within``, each dependency's ``class_ref`` and the
        object references embedded in the class default properties (in
        addition to the base struct/token references handled by ``super``).
        """
        super().resolve()
        pkg = self.export.package
        self.class_within_item = resolve_item(pkg, self.class_within)
        for dep in self.dependencies:
            dep["_class_item"] = resolve_item(pkg, dep["class_ref"])
        for tag in self.default_properties:
            _resolve_tag_object_refs(tag, pkg)

    def link(self) -> None:
        """Re-derive class-level object references from resolved item pointers."""
        super().link()
        pkg = self.export.package
        if hasattr(self, "class_within_item"):
            self.class_within = link_item(pkg, self.class_within_item)
        for dep in self.dependencies:
            if "_class_item" in dep:
                dep["class_ref"] = link_item(pkg, dep["_class_item"])
        for tag in self.default_properties:
            _link_tag_object_refs(tag, pkg)

    def remap_name_indices(self, index_map: Dict[int, int]) -> None:
        """Remap stored name indices using the given mapping.

        Args:
            index_map (Dict[int, int]): Mapping from old to new name indices.
        """
        super().remap_name_indices(index_map)
        pkg = self.export.package
        for tag in self.default_properties:
            _remap_property_tag_data(tag, index_map, pkg)

    def deduplicate_names(self) -> None:
        """Re-resolve name pointers against the deduplicated name table."""
        super().deduplicate_names()
        pkg = self.export.package

        def _resolve(n):
            """Resolve a name entry against the package name table.

            Args:
                n: The name entry to resolve, or None.

            Returns:
                The re-resolved name entry, or None.
            """
            return pkg.find_name(n.name) if n else None

        self.class_config_name_entry = _resolve(self.class_config_name_entry)
        self._default_none_name = _resolve(self._default_none_name)
        self.package_import_names = [_resolve(n) for n in self.package_import_names]
        self.hide_category_names = [_resolve(n) for n in self.hide_category_names]
        for tag in self.default_properties:
            tag.tag_name = _resolve(tag.tag_name)
            tag.struct_name_entry = _resolve(tag.struct_name_entry)
            tag.trailing_none_name = _resolve(tag.trailing_none_name)

    def dump(self) -> str:
        """Return a human-readable summary of the class.

        Returns:
            str: A single-line description of the class.
        """
        fname = self.friendly_name.name if self.friendly_name else "?"
        cfg = self._resolve_name_index(self.class_config_name)
        return (
            f"UnClass: {self.export.object_name_string} "
            f"(Friendly Name: {fname}, "
            f"ClassFlags: 0x{self.class_flags:08X}, "
            f"Config: {cfg}, "
            f"Deps: {len(self.dependencies)}, "
            f"Imports: {len(self.package_imports)}, "
            f"HideCat: {len(self.hide_categories)}, "
            f"DefaultProps: {len(self.default_properties)})"
        )


class UnConst(UnField):
    """Represents a constant definition in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the constant definition.

        Args:
            export (UnExport): The export this constant belongs to.
        """
        super().__init__(export)
        self.value: str = ""

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the constant definition from a binary stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        # The value is a length-prefixed FString (compact-index count of the
        # byte payload, which includes the trailing null terminator).
        length = read_index(reader)
        raw = reader.read(length)
        self.value = raw.decode("latin-1").rstrip("\x00")

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the constant definition to a binary stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): Absolute stream offset of the object data.
        """
        super()._serialize(writer, stream_position)
        encoded = self.value.encode("latin-1") + b"\x00"
        write_index(writer, len(encoded))
        writer.write(encoded)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the constant definition to a dictionary.

        Returns:
            Dict[str, Any]: The constant data as a dictionary.
        """
        d = super().to_dict()
        d["value"] = self.value
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the constant definition from a dictionary.

        Args:
            data (Dict[str, Any]): The constant data to load.
        """
        super().from_dict(data)
        self.value = data.get("value", "")

    def dump(self) -> str:
        """Return a human-readable summary of the constant.

        Returns:
            str: A single-line description of the constant.
        """
        return f"UnConst: {self.export.object_name_string}, Value: {self.value}"


class UnEnum(UnField):
    """Represents an enumeration definition in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the enumeration definition.

        Args:
            export (UnExport): The export this enumeration belongs to.
        """
        super().__init__(export)
        self.names: List["UnName"] = []

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the enumeration definition from a binary stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        length = read_index(reader)
        self.names.clear()
        for _ in range(length):
            idx = read_index(reader)
            self.names.append(self.export.package.names[idx])

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the enumeration definition to a binary stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): Absolute stream offset of the object data.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, len(self.names))
        for name_entry in self.names:
            write_index(writer, self.export.package.name_index(name_entry))

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the enumeration definition to a dictionary.

        Returns:
            Dict[str, Any]: The enumeration data as a dictionary.
        """
        d = super().to_dict()
        d["names"] = [n.name for n in self.names]
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the enumeration definition from a dictionary.

        Args:
            data (Dict[str, Any]): The enumeration data to load.

        Raises:
            RuntimeError: If an enum name is not found in the package name table.
        """
        super().from_dict(data)
        self.names = []
        for name_str in data.get("names", []):
            un_name = self.export.package.find_name(name_str)
            if un_name is None:
                raise RuntimeError(
                    f"Enum name '{name_str}' not found in package name table."
                )
            self.names.append(un_name)

    def deduplicate_names(self):
        """Re-resolve enum name pointers against the deduplicated name table."""
        super().deduplicate_names()
        pkg = self.export.package
        self.names = [pkg.find_name(n.name) for n in self.names]

    def dump(self) -> str:
        """Return a human-readable summary of the enumeration.

        Returns:
            str: A single-line description of the enumeration.
        """
        dump_str = f"Enum: {self.export.object_name_string}"
        if not self.names:
            return dump_str
        entries = ",".join(n.name for n in self.names)
        return f"{dump_str}({entries})"


class UnTextBuffer(UnObject):
    """Represents a text buffer (source code) in an Unreal package."""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the text buffer.

        Args:
            export (UnExport): The export this text buffer belongs to.
        """
        super().__init__(export)
        self.pos: int = 0
        self.top: int = 0
        self.script_text: str = ""

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the text buffer from a binary stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.pos = read_int(reader)
        self.top = read_int(reader)
        script_length = read_index(reader)
        script_bin = reader.read(script_length)
        self.script_text = script_bin.decode("latin-1").rstrip("\x00")

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the text buffer to a binary stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): Absolute stream offset of the object data.
        """
        super()._serialize(writer, stream_position)
        write_int(writer, self.pos)
        write_int(writer, self.top)
        script_bin = self.script_text.encode("latin-1")
        write_index(writer, len(script_bin) + 1)
        writer.write(script_bin)
        writer.write(b"\x00")

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the text buffer to a dictionary.

        Returns:
            Dict[str, Any]: The text buffer data as a dictionary.
        """
        d = super().to_dict()
        d["pos"] = self.pos
        d["top"] = self.top
        d["script_text"] = self.script_text
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the text buffer from a dictionary.

        Args:
            data (Dict[str, Any]): The text buffer data to load.
        """
        super().from_dict(data)
        self.pos = int(data.get("pos", 0))
        self.top = int(data.get("top", 0))
        self.script_text = data.get("script_text", "")

    def export_xml(self, obj_dict: Dict[str, Any], output_dir: str) -> None:
        """Write script_text to a sidecar .txt file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            output_dir (str): The directory to write the sidecar file into.
        """
        script_text = obj_dict.pop("script_text", "")
        txt_filename = self.export.object_name_string + ".txt"
        txt_subdir = os.path.join(output_dir, "UnTextBuffer")
        os.makedirs(txt_subdir, exist_ok=True)
        txt_path = os.path.join(txt_subdir, txt_filename)
        with open(txt_path, "w", encoding="latin-1", newline="") as f:
            f.write(script_text)
        obj_dict["script_text_file"] = txt_filename

    def import_xml(self, obj_dict: Dict[str, Any], input_dir: str) -> None:
        """Read script_text from a sidecar .txt file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            input_dir (str): The directory to read the sidecar file from.
        """
        txt_filename = obj_dict.pop("script_text_file", "")
        if txt_filename:
            txt_path = os.path.join(input_dir, "UnTextBuffer", txt_filename)
            with open(txt_path, "r", encoding="latin-1", newline="") as f:
                obj_dict["script_text"] = f.read()

    def dump(self) -> str:
        """Return a human-readable summary of the text buffer.

        Returns:
            str: A single-line description of the text buffer.
        """
        return f"UnTextBuffer: Script Size: {len(self.script_text)}"


# ===================================================================== #
#  UProperty and subclasses (property field definitions)
# ===================================================================== #


class UnProperty(UnField):
    """Base class for property field definitions.

    Serialises the property definition: ``ArrayDim`` (int32),
    ``PropertyFlags`` (uint32), ``Category`` (name index), optional
    ``RepOffset`` (uint16) when the ``Net`` flag is set, optional
    ``CommentString`` (string) when the ``CommentString`` flag is set.

    ``ElementSize`` is **not** serialised — it is computed at link time
    in the engine and therefore omitted from the persistent format.

    Subclasses must implement :meth:`parse_item` / :meth:`serialize_item`
    which read/write the *value* the property represents inside
    tagged-property data.  Subclasses may also override
    :meth:`data_to_value` / :meth:`value_to_data` for human-readable
    string conversion (XML roundtrip).
    """

    @abstractmethod
    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read the value this property represents from a stream.

        Must be implemented by every concrete property subclass.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed (0 means
                unbounded).

        Returns:
            bytes: The raw tagged-property value data.
        """
        ...

    @abstractmethod
    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write the value this property represents to a stream.

        Must be implemented by every concrete property subclass.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        ...

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode raw tagged property data to a human-readable string.

        Falls back to a hex representation of the data.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The human-readable value.
        """
        return bytes_to_hex(data) if data else ""

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable string back to raw tagged property data.

        Falls back to decoding a hex representation.

        Args:
            value (str): The human-readable value.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The raw tagged-property value data.
        """
        return hex_to_bytes(value) if value else b""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the property definition.

        Args:
            export (UnExport): The export this property belongs to.
        """
        super().__init__(export)
        self.array_dim: int = 0
        self.property_flags: int = 0
        self.category_name_index: int = 0
        self.category_name_entry: Optional["UnName"] = None
        self.rep_offset: int = 0
        self.comment_string: str = ""

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the property definition from a binary stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.array_dim = read_int(reader)
        self.property_flags = read_uint(reader)
        self.category_name_index = read_index(reader)
        pkg = self.export.package
        if 0 <= self.category_name_index < len(pkg.names):
            self.category_name_entry = pkg.names[self.category_name_index]
        if self.property_flags & int(UnPropertyFlags.Net):
            self.rep_offset = read_word(reader)
        if self.property_flags & int(UnPropertyFlags.CommentString):
            self.comment_string = read_ascii(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the property definition to a binary stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): Absolute stream offset of the object data.
        """
        super()._serialize(writer, stream_position)
        write_int(writer, self.array_dim)
        write_uint(writer, self.property_flags)
        if self.category_name_entry is not None:
            self.category_name_index = self.export.package.name_index(
                self.category_name_entry
            )
        write_index(writer, self.category_name_index)
        if self.property_flags & int(UnPropertyFlags.Net):
            write_word(writer, self.rep_offset)
        if self.property_flags & int(UnPropertyFlags.CommentString):
            write_ascii(writer, self.comment_string)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the property definition to a dictionary.

        Returns:
            Dict[str, Any]: The property data as a dictionary.
        """
        d = super().to_dict()
        d["array_dim"] = self.array_dim
        d["property_flags"] = property_flags_to_string(
            UnPropertyFlags(self.property_flags)
        )
        if self.category_name_entry is not None:
            pkg = self.export.package
            d["category"] = pkg.resolve_name_index(
                pkg.name_index(self.category_name_entry)
            )
        else:
            d["category"] = self._resolve_name_index(self.category_name_index)
        if self.property_flags & int(UnPropertyFlags.Net):
            d["rep_offset"] = self.rep_offset
        if self.comment_string:
            d["comment_string"] = self.comment_string
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the property definition from a dictionary.

        Args:
            data (Dict[str, Any]): The property data to load.
        """
        super().from_dict(data)
        self.array_dim = int(data.get("array_dim", 0))
        pf = data.get("property_flags", "")
        if isinstance(pf, str) and pf and not pf.isdigit():
            self.property_flags = int(string_to_property_flags(pf))
        else:
            self.property_flags = int(pf) if pf else 0
        cat_str = data.get("category", "")
        if cat_str:
            pkg = self.export.package
            idx = pkg.link_name_index(cat_str)
            self.category_name_index = idx
            if 0 <= idx < len(pkg.names):
                self.category_name_entry = pkg.names[idx]
        else:
            self.category_name_index = 0
            self.category_name_entry = None
        self.rep_offset = int(data.get("rep_offset", 0))
        self.comment_string = data.get("comment_string", "")

    def deduplicate_names(self) -> None:
        """Re-resolve the category name against the deduplicated name table."""
        super().deduplicate_names()
        pkg = self.export.package
        if self.category_name_entry is not None:
            self.category_name_entry = pkg.find_name(self.category_name_entry.name)

    def dump(self) -> str:
        """Return a human-readable summary of the property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnByteProperty(UnProperty):
    """Byte or enum property. Extra: Enum (object ref)."""

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read one byte.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The single byte read.
        """
        return reader.read(1)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write one byte.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        writer.write(data[:1])

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode a byte value to a human-readable string.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The decoded value, or a hex fallback.
        """
        if len(data) == 1:
            return str(data[0])
        return bytes_to_hex(data) if data else ""

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable string back to a byte value.

        Args:
            value (str): The human-readable value.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded value, or a hex fallback.
        """
        try:
            return pack_byte(int(value))
        except (ValueError, TypeError):
            return hex_to_bytes(value) if value else b""

    def __init__(self, export: "UnExport") -> None:
        """Initialise the byte property definition.

        Args:
            export (UnExport): The export this property belongs to.
        """
        super().__init__(export)
        self.enum_reference: int = 0
        self.enum_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the byte property definition from a binary stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.enum_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the byte property definition to a binary stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): Absolute stream offset of the object data.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.enum_reference)

    def resolve(self) -> None:
        """Resolve the enum object reference to a package item."""
        super().resolve()
        self.enum_item = resolve_item(self.export.package, self.enum_reference)

    def link(self) -> None:
        """Link the enum item back to an object reference."""
        super().link()
        self.enum_reference = link_item(self.export.package, self.enum_item)

    def clear_resolved(self) -> None:
        """Clear the resolved enum item pointer."""
        super().clear_resolved()
        self.enum_item = None

    def clear_links(self) -> None:
        """Clear the enum object reference."""
        super().clear_links()
        self.enum_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the byte property definition to a dictionary.

        Returns:
            Dict[str, Any]: The property data as a dictionary.
        """
        d = super().to_dict()
        d["enum"] = self._resolve_object_ref(self.enum_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the byte property definition from a dictionary.

        Args:
            data (Dict[str, Any]): The property data to load.
        """
        super().from_dict(data)
        self.enum_reference = self._link_object_ref(data.get("enum", ""))

    def dump(self) -> str:
        """Return a human-readable summary of the byte property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnByteProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnIntProperty(UnProperty):
    """32-bit signed integer property. No extra serialised data."""

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read 4-byte signed integer.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The four bytes read.
        """
        return reader.read(4)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write 4-byte signed integer.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        writer.write(data[:4])

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode a 4-byte integer value to a human-readable string.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The decoded value, or a hex fallback.
        """
        if len(data) == 4:
            return str(unpack_int(data))
        return bytes_to_hex(data) if data else ""

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable string back to a 4-byte integer value.

        Args:
            value (str): The human-readable value.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded value, or a hex fallback.
        """
        try:
            return pack_int(int(value))
        except (ValueError, TypeError):
            return hex_to_bytes(value) if value else b""

    def dump(self) -> str:
        """Return a human-readable summary of the integer property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnIntProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnBoolProperty(UnProperty):
    """Boolean property. BitMask is NOT serialised to disk."""

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read one byte (bool flag).

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The single byte read.
        """
        return reader.read(1)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write one byte (bool flag).

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        writer.write(data[:1])

    def dump(self) -> str:
        """Return a human-readable summary of the boolean property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnBoolProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnFloatProperty(UnProperty):
    """IEEE 32-bit float property. No extra serialised data."""

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read 4-byte IEEE float.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The 4 bytes read.
        """
        return reader.read(4)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write 4-byte IEEE float.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        writer.write(data[:4])

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode a 4-byte IEEE float value to a human-readable string.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The decoded value, or a hex fallback.
        """
        if len(data) == 4:
            return format_float32(unpack_float(data))
        return bytes_to_hex(data) if data else ""

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable string back to a 4-byte IEEE float value.

        Args:
            value (str): The human-readable value.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded value, or a hex fallback.
        """
        try:
            return pack_float(float(value))
        except (ValueError, TypeError):
            return hex_to_bytes(value) if value else b""

    def dump(self) -> str:
        """Return a human-readable summary of the float property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnFloatProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnObjectProperty(UnProperty):
    """Object reference property. Extra: PropertyClass (object ref).

    Tagged property data: compact-index object reference.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read compact-index object reference.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The encoded compact-index object reference.
        """
        buf = io.BytesIO()
        ref = read_index(reader)
        write_index(buf, ref)
        return buf.getvalue()

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write compact-index object reference.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        buf = io.BytesIO(data)
        ref = read_index(buf)
        write_index(writer, ref)

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode a compact-index object reference to a human-readable string.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The resolved object reference, or the raw index.
        """
        if not data:
            return ""
        buf = io.BytesIO(data)
        ref = read_index(buf)
        if package is not None:
            return package.resolve_item_ref(ref)
        return str(ref)

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable object reference to a compact-index value.

        Args:
            value (str): The human-readable object reference.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded compact-index object reference.
        """
        if package is not None and value:
            ref = package.link_item_ref(value)
        else:
            try:
                ref = int(value) if value else 0
            except (ValueError, TypeError):
                ref = 0
        buf = io.BytesIO()
        write_index(buf, ref)
        return buf.getvalue()

    def __init__(self, export: "UnExport") -> None:
        """Initialize the object property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.property_class_reference: int = 0
        self.property_class_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the property class reference from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.property_class_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the property class reference to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.property_class_reference)

    def resolve(self) -> None:
        """Resolve the property class item pointer from its reference index."""
        super().resolve()
        self.property_class_item = resolve_item(
            self.export.package, self.property_class_reference
        )

    def link(self) -> None:
        """Link the property class reference index from its resolved item."""
        super().link()
        self.property_class_reference = link_item(
            self.export.package, self.property_class_item
        )

    def clear_resolved(self) -> None:
        """Clear the resolved property class item pointer."""
        super().clear_resolved()
        self.property_class_item = None

    def clear_links(self) -> None:
        """Clear the property class reference index."""
        super().clear_links()
        self.property_class_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the object property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["property_class"] = self._resolve_object_ref(self.property_class_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the object property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.property_class_reference = self._link_object_ref(
            data.get("property_class", "")
        )

    def dump(self) -> str:
        """Return a human-readable summary of the object property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnObjectProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnClassProperty(UnObjectProperty):
    """Class reference property. Extra: MetaClass (object ref)."""

    def __init__(self, export: "UnExport") -> None:
        """Initialize the class property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.meta_class_reference: int = 0
        self.meta_class_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the meta class reference from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.meta_class_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the meta class reference to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.meta_class_reference)

    def resolve(self) -> None:
        """Resolve the meta class item pointer from its reference index."""
        super().resolve()
        self.meta_class_item = resolve_item(
            self.export.package, self.meta_class_reference
        )

    def link(self) -> None:
        """Link the meta class reference index from its resolved item."""
        super().link()
        self.meta_class_reference = link_item(self.export.package, self.meta_class_item)

    def clear_resolved(self) -> None:
        """Clear the resolved meta class item pointer."""
        super().clear_resolved()
        self.meta_class_item = None

    def clear_links(self) -> None:
        """Clear the meta class reference index."""
        super().clear_links()
        self.meta_class_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the class property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["meta_class"] = self._resolve_object_ref(self.meta_class_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the class property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.meta_class_reference = self._link_object_ref(data.get("meta_class", ""))

    def dump(self) -> str:
        """Return a human-readable summary of the class property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnClassProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnNameProperty(UnProperty):
    """Name reference property. No extra serialised data.

    Tagged property data: compact-index name table reference.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read compact-index name reference.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The encoded compact-index name reference.
        """
        buf = io.BytesIO()
        idx = read_index(reader)
        write_index(buf, idx)
        return buf.getvalue()

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write compact-index name reference.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        buf = io.BytesIO(data)
        idx = read_index(buf)
        write_index(writer, idx)

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode a compact-index name reference to a human-readable string.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The resolved name, or the raw index.
        """
        if not data:
            return ""
        buf = io.BytesIO(data)
        idx = read_index(buf)
        if package is not None:
            return package.resolve_name_index(idx)
        return str(idx)

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable name to a compact-index name reference.

        Args:
            value (str): The human-readable name.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded compact-index name reference.
        """
        if package is not None and value:
            idx = package.link_name_index(value)
        else:
            try:
                idx = int(value) if value else 0
            except (ValueError, TypeError):
                idx = 0
        buf = io.BytesIO()
        write_index(buf, idx)
        return buf.getvalue()

    def dump(self) -> str:
        """Return a human-readable summary of the name property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnNameProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnStrProperty(UnProperty):
    """String property. No extra serialised data."""

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read string (compact-index length + chars).

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The encoded string data.
        """
        s = _UnString()
        s.parse(reader)
        return s.serialize_to_bytes()

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write string (compact-index length + chars).

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        s = _UnString()
        s.parse_from_bytes(data)
        s.serialize(writer)

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode string data to a human-readable string.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The decoded string value.
        """
        s = _UnString()
        s.parse_from_bytes(data)
        return s.value

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode a human-readable string to string data.

        Args:
            value (str): The human-readable string value.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded string data.
        """
        return _UnString(value).serialize_to_bytes()

    def dump(self) -> str:
        """Return a human-readable summary of the string property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnStrProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnFixedArrayProperty(UnProperty):
    """Fixed-length array. Extra: Inner (object ref), Count (int32).

    Tagged property data: raw element data (count is fixed in the property definition).
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read ``Count`` elements via the inner property's ``parse_item``.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The concatenated encoded element data.

        Raises:
            RuntimeError: If Inner is not resolved or is not a UnProperty.
        """
        if self.inner_item is None:
            raise RuntimeError(
                f"UnFixedArrayProperty.parse_item: Inner is not resolved "
                f"for {self.export.object_name_string}"
            )
        inner_obj = self.inner_item.object
        if not isinstance(inner_obj, UnProperty):
            raise RuntimeError(
                f"UnFixedArrayProperty.parse_item: Inner object is not a "
                f"UnProperty for {self.export.object_name_string}"
            )
        buf = io.BytesIO()
        per_elem = size // self.count if self.count > 0 else 0
        for i in range(self.count):
            elem = inner_obj.parse_item(reader, per_elem)
            buf.write(elem)
        return buf.getvalue()

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write ``Count`` elements via the inner property's ``serialize_item``.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.

        Raises:
            RuntimeError: If Inner is not resolved or is not a UnProperty.
        """
        if self.inner_item is None:
            raise RuntimeError(
                f"UnFixedArrayProperty.serialize_item: Inner is not resolved "
                f"for {self.export.object_name_string}"
            )
        inner_obj = self.inner_item.object
        if not isinstance(inner_obj, UnProperty):
            raise RuntimeError(
                f"UnFixedArrayProperty.serialize_item: Inner object is not a "
                f"UnProperty for {self.export.object_name_string}"
            )
        if self.count > 0:
            elem_size = len(data) // self.count
            for i in range(self.count):
                inner_obj.serialize_item(
                    writer, data[i * elem_size : (i + 1) * elem_size]
                )
        else:
            writer.write(data)

    def __init__(self, export: "UnExport") -> None:
        """Initialize the fixed-array property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.inner_reference: int = 0
        self.inner_item: Optional["UnPackageItem"] = None
        self.count: int = 0

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the inner reference and count from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.inner_reference = read_index(reader)
        self.count = read_int(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the inner reference and count to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.inner_reference)
        write_int(writer, self.count)

    def resolve(self) -> None:
        """Resolve the inner-property item pointer from its reference index."""
        super().resolve()
        self.inner_item = resolve_item(self.export.package, self.inner_reference)

    def link(self) -> None:
        """Link the inner-property reference index from its resolved item."""
        super().link()
        self.inner_reference = link_item(self.export.package, self.inner_item)

    def clear_resolved(self) -> None:
        """Clear the resolved inner-property item pointer."""
        super().clear_resolved()
        self.inner_item = None

    def clear_links(self) -> None:
        """Clear the inner-property reference index."""
        super().clear_links()
        self.inner_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the fixed-array property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["inner"] = self._resolve_object_ref(self.inner_reference)
        d["count"] = self.count
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the fixed-array property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.inner_reference = self._link_object_ref(data.get("inner", ""))
        self.count = int(data.get("count", 0))

    def dump(self) -> str:
        """Return a human-readable summary of the fixed-array property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnFixedArrayProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Count={self.count}, "
            f"Flags=0x{self.property_flags:08X})"
        )


class UnArrayProperty(UnProperty):
    """Dynamic array. Extra: Inner (object ref).

    Tagged property data: compact-index count + element data.
    Elements are decoded based on the inner property type.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read compact-index count then that many inner-property elements.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The encoded count prefix and element data.

        Raises:
            RuntimeError: If Inner is not resolved or is not a UnProperty.
        """
        if self.inner_item is None:
            raise RuntimeError(
                f"UnArrayProperty.parse_item: Inner is not resolved "
                f"for {self.export.object_name_string}"
            )
        inner_obj = self.inner_item.object
        if not isinstance(inner_obj, UnProperty):
            raise RuntimeError(
                f"UnArrayProperty.parse_item: Inner object is not a "
                f"UnProperty for {self.export.object_name_string}"
            )
        start = reader.tell()
        buf = io.BytesIO()
        n = read_index(reader)
        write_index(buf, n)
        if n > 0:
            remaining = size - (reader.tell() - start)
            per_elem = remaining // n if n > 0 else 0
            for i in range(n):
                elem = inner_obj.parse_item(reader, per_elem)
                buf.write(elem)
        return buf.getvalue()

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write compact-index count then that many inner-property elements.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.

        Raises:
            RuntimeError: If Inner is not resolved or is not a UnProperty.
        """
        if self.inner_item is None:
            raise RuntimeError(
                f"UnArrayProperty.serialize_item: Inner is not resolved "
                f"for {self.export.object_name_string}"
            )
        inner_obj = self.inner_item.object
        if not isinstance(inner_obj, UnProperty):
            raise RuntimeError(
                f"UnArrayProperty.serialize_item: Inner object is not a "
                f"UnProperty for {self.export.object_name_string}"
            )
        buf = io.BytesIO(data)
        n = read_index(buf)
        write_index(writer, n)
        if n > 0:
            elem_data = buf.read()
            elem_size = len(elem_data) // n if n > 0 else 0
            for i in range(n):
                inner_obj.serialize_item(
                    writer, elem_data[i * elem_size : (i + 1) * elem_size]
                )

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode array data by skipping the count prefix and returning hex.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The element data as a hex string, or empty.
        """
        if not data:
            return ""
        buf = io.BytesIO(data)
        _count = read_index(buf)
        rest = buf.read()
        return bytes_to_hex(rest) if rest else ""

    @staticmethod
    def value_to_data(
        value: str, package: Optional["UnPackage"] = None, count: int = 0
    ) -> bytes:
        """Encode array data by prepending the count prefix to element data.

        Args:
            value (str): The element data as a hex string.
            package (Optional["UnPackage"]): The owning package. Defaults to None.
            count (int): The element count prefix. Defaults to 0.

        Returns:
            bytes: The encoded count prefix and element data.
        """
        buf = io.BytesIO()
        write_index(buf, count)
        if value:
            buf.write(hex_to_bytes(value))
        return buf.getvalue()

    @staticmethod
    def extract_count(data: bytes) -> int:
        """Read the count prefix from raw tagged property data.

        Args:
            data (bytes): The raw tagged-property value data.

        Returns:
            int: The decoded element count.
        """
        if not data:
            return 0
        buf = io.BytesIO(data)
        return read_index(buf)

    def __init__(self, export: "UnExport") -> None:
        """Initialize the dynamic-array property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.inner_reference: int = 0
        self.inner_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the inner reference from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.inner_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the inner reference to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.inner_reference)

    def resolve(self) -> None:
        """Resolve the inner-property item pointer from its reference index."""
        super().resolve()
        self.inner_item = resolve_item(self.export.package, self.inner_reference)

    def link(self) -> None:
        """Link the inner-property reference index from its resolved item."""
        super().link()
        self.inner_reference = link_item(self.export.package, self.inner_item)

    def clear_resolved(self) -> None:
        """Clear the resolved inner-property item pointer."""
        super().clear_resolved()
        self.inner_item = None

    def clear_links(self) -> None:
        """Clear the inner-property reference index."""
        super().clear_links()
        self.inner_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the dynamic-array property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["inner"] = self._resolve_object_ref(self.inner_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the dynamic-array property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.inner_reference = self._link_object_ref(data.get("inner", ""))

    def dump(self) -> str:
        """Return a human-readable summary of the dynamic-array property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnArrayProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnMapProperty(UnProperty):
    """Dynamic map. Extra: Key (object ref), Value (object ref).

    Map item serialization is a no-op in the engine; we read/write raw
    bytes for roundtrip fidelity.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read raw bytes (engine map serialization is a no-op).

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The raw bytes read.
        """
        return reader.read(size)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write raw bytes (engine map serialization is a no-op).

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        writer.write(data)

    def __init__(self, export: "UnExport") -> None:
        """Initialize the dynamic-map property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.key_reference: int = 0
        self.key_item: Optional["UnPackageItem"] = None
        self.value_reference: int = 0
        self.value_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the key and value references from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.key_reference = read_index(reader)
        self.value_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the key and value references to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.key_reference)
        write_index(writer, self.value_reference)

    def resolve(self) -> None:
        """Resolve the key and value item pointers from their reference indices."""
        super().resolve()
        self.key_item = resolve_item(self.export.package, self.key_reference)
        self.value_item = resolve_item(self.export.package, self.value_reference)

    def link(self) -> None:
        """Link the key and value reference indices from their resolved items."""
        super().link()
        self.key_reference = link_item(self.export.package, self.key_item)
        self.value_reference = link_item(self.export.package, self.value_item)

    def clear_resolved(self) -> None:
        """Clear the resolved key and value item pointers."""
        super().clear_resolved()
        self.key_item = None
        self.value_item = None

    def clear_links(self) -> None:
        """Clear the key and value reference indices."""
        super().clear_links()
        self.key_reference = 0
        self.value_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the dynamic-map property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["key"] = self._resolve_object_ref(self.key_reference)
        d["value"] = self._resolve_object_ref(self.value_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the dynamic-map property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.key_reference = self._link_object_ref(data.get("key", ""))
        self.value_reference = self._link_object_ref(data.get("value", ""))

    def dump(self) -> str:
        """Return a human-readable summary of the dynamic-map property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnMapProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnStructProperty(UnProperty):
    """Embedded struct property. Extra: Struct (object ref).

    For UT2004 package compatibility, uses raw sequential (native)
    serialisation for Vector/Rotator/Color and tagged-property
    serialisation for everything else.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read struct value data.

        Uses native (raw sequential) serialisation for Vector/Rotator/Color
        and tagged-property serialisation for everything else.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The encoded struct value data.

        Raises:
            RuntimeError: If ``struct_item`` is not resolved.
        """
        if self.struct_item is None:
            raise RuntimeError(
                f"UnStructProperty.parse_item: Struct is not resolved "
                f"for {self.export.object_name_string}"
            )
        struct_obj = self.struct_item.object
        struct_name = self.struct_item.object_name.name
        if isinstance(struct_obj, UnStruct) and struct_name in (
            "Vector",
            "Rotator",
            "Color",
        ):
            return struct_obj.parse_bin(reader, size)
        # Tagged-property format — read raw bytes
        return reader.read(size)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write struct value data.

        Uses native (raw sequential) serialisation for Vector/Rotator/Color
        and tagged-property serialisation for everything else.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.

        Raises:
            RuntimeError: If ``struct_item`` is not resolved.
        """
        if self.struct_item is None:
            raise RuntimeError(
                f"UnStructProperty.serialize_item: Struct is not resolved "
                f"for {self.export.object_name_string}"
            )
        struct_obj = self.struct_item.object
        struct_name = self.struct_item.object_name.name
        if isinstance(struct_obj, UnStruct) and struct_name in (
            "Vector",
            "Rotator",
            "Color",
        ):
            struct_obj.serialize_bin(writer, data)
        else:
            # Tagged-property format — write raw bytes
            writer.write(data)

    def __init__(self, export: "UnExport") -> None:
        """Initialize the struct property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.struct_reference: int = 0
        self.struct_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the struct reference from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.struct_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the struct reference to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.struct_reference)

    def resolve(self) -> None:
        """Resolve the struct item pointer from its reference index."""
        super().resolve()
        self.struct_item = resolve_item(self.export.package, self.struct_reference)

    def link(self) -> None:
        """Link the struct reference index from its resolved item."""
        super().link()
        self.struct_reference = link_item(self.export.package, self.struct_item)

    def clear_resolved(self) -> None:
        """Clear the resolved struct item pointer."""
        super().clear_resolved()
        self.struct_item = None

    def clear_links(self) -> None:
        """Clear the struct reference index."""
        super().clear_links()
        self.struct_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the struct property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["struct"] = self._resolve_object_ref(self.struct_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the struct property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.struct_reference = self._link_object_ref(data.get("struct", ""))

    def dump(self) -> str:
        """Return a human-readable summary of the struct property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnStructProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnDelegateProperty(UnProperty):
    """Delegate property. Extra: Function (object ref).

    Value data: a delegate record containing an object compact-index
    reference and a function-name compact-index reference.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read delegate value: object compact-index + function name index.

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The encoded delegate record.
        """
        buf = io.BytesIO()
        obj_ref = read_index(reader)
        write_index(buf, obj_ref)
        func_name = read_index(reader)
        write_index(buf, func_name)
        return buf.getvalue()

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write delegate value: object compact-index + function name index.

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        buf = io.BytesIO(data)
        obj_ref = read_index(buf)
        write_index(writer, obj_ref)
        func_name = read_index(buf)
        write_index(writer, func_name)

    @staticmethod
    def data_to_value(data: bytes, package: Optional["UnPackage"] = None) -> str:
        """Decode a delegate record to a human-readable ``objref:function`` string.

        The value pairs a compact-index object reference (the delegate's bound
        object) with a compact-index name reference (the function name),
        rendered as ``<object-ref>:<function-name>``.

        Args:
            data (bytes): The raw tagged-property value data.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            str: The decoded ``objref:function`` value, or ``""`` when empty.
        """
        if not data:
            return ""
        buf = io.BytesIO(data)
        obj_ref = read_index(buf)
        func_name = read_index(buf)
        if package is not None:
            obj_str = package.resolve_item_ref(obj_ref)
            func_str = package.resolve_name_index(func_name)
        else:
            obj_str = str(obj_ref)
            func_str = str(func_name)
        return f"{obj_str}:{func_str}"

    @staticmethod
    def value_to_data(value: str, package: Optional["UnPackage"] = None) -> bytes:
        """Encode an ``objref:function`` string back to a delegate record.

        Inverse of :meth:`data_to_value`.  The object-reference portion may be
        empty (a null delegate object); the function-name portion follows the
        first ``:`` separator.

        Args:
            value (str): The ``objref:function`` value.
            package (Optional["UnPackage"]): The owning package. Defaults to None.

        Returns:
            bytes: The encoded delegate record, or ``b""`` when empty.
        """
        if not value:
            return b""
        obj_str, _, func_str = value.partition(":")
        if package is not None:
            obj_ref = package.link_item_ref(obj_str) if obj_str else 0
            func_name = package.link_name_index(func_str) if func_str else 0
        else:
            obj_ref = int(obj_str) if obj_str else 0
            func_name = int(func_str) if func_str else 0
        buf = io.BytesIO()
        write_index(buf, obj_ref)
        write_index(buf, func_name)
        return buf.getvalue()

    def __init__(self, export: "UnExport") -> None:
        """Initialize the delegate property.

        Args:
            export (UnExport): The export entry owning this object.
        """
        super().__init__(export)
        self.function_reference: int = 0
        self.function_item: Optional["UnPackageItem"] = None

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the function reference from the stream.

        Args:
            reader (BinaryIO): The stream to read from.
        """
        super()._parse(reader)
        self.function_reference = read_index(reader)

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialize the function reference to the stream.

        Args:
            writer (BinaryIO): The stream to write to.
            stream_position (int): The current position in the output stream.
        """
        super()._serialize(writer, stream_position)
        write_index(writer, self.function_reference)

    def resolve(self) -> None:
        """Resolve the function item pointer from its reference index."""
        super().resolve()
        self.function_item = resolve_item(self.export.package, self.function_reference)

    def link(self) -> None:
        """Link the function reference index from its resolved item."""
        super().link()
        self.function_reference = link_item(self.export.package, self.function_item)

    def clear_resolved(self) -> None:
        """Clear the resolved function item pointer."""
        super().clear_resolved()
        self.function_item = None

    def clear_links(self) -> None:
        """Clear the function reference index."""
        super().clear_links()
        self.function_reference = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the delegate property to a dictionary.

        Returns:
            Dict[str, Any]: The serialized property data.
        """
        d = super().to_dict()
        d["function"] = self._resolve_object_ref(self.function_reference)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the delegate property from a dictionary.

        Args:
            data (Dict[str, Any]): The serialized property data.
        """
        super().from_dict(data)
        self.function_reference = self._link_object_ref(data.get("function", ""))

    def dump(self) -> str:
        """Return a human-readable summary of the delegate property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnDelegateProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


class UnPointerProperty(UnProperty):
    """Platform-dependent pointer-sized int. No extra serialised data.

    Pointers are always transient: the value is a 4-byte zero.
    """

    def parse_item(self, reader: BinaryIO, size: int) -> bytes:
        """Read 4-byte zero (pointer, always transient).

        Args:
            reader (BinaryIO): The stream to read from.
            size (int): Maximum byte count that may be consumed.

        Returns:
            bytes: The 4 bytes read.
        """
        return reader.read(4)

    def serialize_item(self, writer: BinaryIO, data: bytes) -> None:
        """Write 4-byte zero (pointer, always transient).

        Args:
            writer (BinaryIO): The stream to write to.
            data (bytes): The raw tagged-property value data.
        """
        writer.write(data[:4] if len(data) >= 4 else pack_int(0))

    def dump(self) -> str:
        """Return a human-readable summary of the pointer property.

        Returns:
            str: A single-line description of the property.
        """
        return (
            f"UnPointerProperty: {self.export.object_name_string} "
            f"(ArrayDim={self.array_dim}, Flags=0x{self.property_flags:08X})"
        )


# ===================================================================== #
#  Array element decode/encode helpers
# ===================================================================== #


def _resolve_via_ref(
    item: Optional["UnPackageItem"],
    ref: int,
    owner_pkg: "UnPackage",
) -> Optional["UnPackageItem"]:
    """Return *item* when set, else look it up in *owner_pkg* by *ref*.

    Used when walking object graphs during XML import — at that point only
    integer references (``children_reference``, ``next_reference``,
    ``super_index``, ``inner_reference``, ``struct_reference``) are
    populated; the item pointers are filled in later by
    :meth:`UnPackage.resolve_objects`.
    """
    if item is not None:
        return item
    if ref == 0 or owner_pkg is None:
        return None
    try:
        if ref > 0 and ref - 1 < len(owner_pkg.exports):
            return owner_pkg.exports[ref - 1]
        if ref < 0 and -ref - 1 < len(owner_pkg.imports):
            return owner_pkg.imports[-ref - 1]
    except (IndexError, AttributeError):
        return None
    return None


def _find_array_inner_info(
    tag_name: str,
    package: "UnPackage",
    parent_struct_name: str = "",
) -> tuple:
    """Find the inner type class name and struct ref for an ArrayProperty.

    Returns ``(inner_class_name, struct_ref)`` where *struct_ref* is the
    object_name_string of the struct definition (for StructProperty inner).

    When *parent_struct_name* is provided, the search is scoped to the
    children of that struct definition (matching how tagged-property
    lookups walk the property link chain).  Otherwise the lookup falls
    back to a global search across exports.

    Walks the struct graph via :func:`_resolve_via_ref` so it works both
    after ``resolve_objects()`` (uses item pointers) and during XML import
    (falls back to integer references).
    """

    def _extract_inner_info(
        arr_prop: "UnArrayProperty", owner_pkg: "UnPackage"
    ) -> tuple:
        """Extract the inner class name and struct ref from an array property.

        Args:
            arr_prop ("UnArrayProperty"): The array property to inspect.
            owner_pkg ("UnPackage"): The package owning the array property.

        Returns:
            tuple: A ``(inner_class_name, struct_ref)`` pair; empty strings
                when the inner property or struct cannot be resolved.
        """
        inner = _resolve_via_ref(
            arr_prop.inner_item, arr_prop.inner_reference, owner_pkg
        )
        if inner is None:
            return ("", "")
        inner_cls = inner.class_name_string
        struct_ref = ""
        if inner_cls.endswith(".StructProperty"):
            inner_obj = inner.object
            if isinstance(inner_obj, UnStructProperty):
                si = _resolve_via_ref(
                    inner_obj.struct_item, inner_obj.struct_reference, owner_pkg
                )
                if si is not None:
                    struct_ref = si.object_name_string
        return (inner_cls, struct_ref)

    if parent_struct_name:
        bare = parent_struct_name.lstrip("+-")
        for pkg in [package] + list(package.imported_packages.values()):
            for exp in pkg.exports:
                if (
                    exp.object_name_string == bare
                    or exp.object_name.name == bare.split(".")[-1]
                ):
                    if not isinstance(exp.object, UnStruct):
                        continue
                    visited: List["UnStruct"] = []
                    cur: Optional["UnStruct"] = exp.object
                    while cur is not None and cur not in visited:
                        visited.append(cur)
                        child = _resolve_via_ref(
                            cur.children, cur.children_reference, pkg
                        )
                        while child is not None:
                            cobj = child.object
                            if child.object_name.name == tag_name and isinstance(
                                cobj, UnArrayProperty
                            ):
                                return _extract_inner_info(cobj, pkg)
                            if isinstance(cobj, UnField):
                                child = _resolve_via_ref(
                                    cobj.next_item, cobj.next_reference, pkg
                                )
                            else:
                                child = None
                        # Walk super struct
                        super_item = _resolve_via_ref(
                            cur.super_item, cur.super_index, pkg
                        )
                        if super_item is not None and isinstance(
                            super_item.object, UnStruct
                        ):
                            cur = super_item.object
                        else:
                            cur = None
                    break  # Found struct, stop searching packages
        return ("", "")

    for pkg in [package] + list(package.imported_packages.values()):
        for exp in pkg.exports:
            if (
                isinstance(exp.object, UnArrayProperty)
                and exp.object_name.name == tag_name
            ):
                return _extract_inner_info(exp.object, pkg)
    return ("", "")


def _decode_array_elements(
    data: bytes,
    inner_type: str,
    package: "UnPackage",
    struct_ref: str = "",
) -> Optional[List[Any]]:
    """Decode array element data into a list of values.

    For most types, values are strings.  For StructProperty, values are
    dicts from ``_decode_struct_data()``.
    """
    if not data:
        return []

    buf = io.BytesIO(data)
    elements: List[Any] = []

    if inner_type.endswith(".ByteProperty"):
        while buf.tell() < len(data):
            elements.append(str(read_byte(buf)))
    elif inner_type.endswith(".IntProperty"):
        while buf.tell() < len(data):
            if len(data) - buf.tell() < 4:
                break
            elements.append(str(unpack_int(buf.read(4))))
    elif inner_type.endswith(".FloatProperty"):
        while buf.tell() < len(data):
            if len(data) - buf.tell() < 4:
                break
            elements.append(format_float32(unpack_float(buf.read(4))))
    elif inner_type.endswith(".StrProperty"):
        while buf.tell() < len(data):
            length = read_index(buf)
            if length > 0:
                raw = buf.read(length)
                elements.append(raw.decode("latin-1").rstrip("\x00"))
            else:
                elements.append("")
    elif inner_type.endswith(".ObjectProperty") or inner_type.endswith(
        ".ClassProperty"
    ):
        while buf.tell() < len(data):
            ref = read_index(buf)
            elements.append(package.resolve_item_ref(ref))
    elif inner_type.endswith(".NameProperty"):
        while buf.tell() < len(data):
            idx = read_index(buf)
            elements.append(package.resolve_name_index(idx))
    elif inner_type.endswith(".StructProperty"):
        # Struct array elements use either native (raw sequential) format
        # for Vector/Rotator/Color or tagged-property format for everything
        # else.  Decode each element accordingly.
        if not struct_ref:
            return None
        if _is_native_serialize_struct(struct_ref):
            # Sequential native struct elements: read field-by-field via streaming
            try:
                while buf.tell() < len(data):
                    elem = _decode_struct_streaming(buf, struct_ref, package)
                    if elem is None:
                        return None
                    elements.append(elem)
            except Exception:
                return None
        else:
            # Tagged struct elements: each ends with the None terminator.
            # Pass struct_ref as parent_struct_name so that nested
            # ArrayProperty inner-type lookups find the correct field.
            try:
                while buf.tell() < len(data):
                    elem = _decode_tagged_struct_streaming(
                        buf, package, parent_struct_name=struct_ref
                    )
                    if elem is None:
                        return None
                    elements.append(elem)
            except Exception:
                return None
    else:
        return None

    return elements


def _encode_array_elements(
    elements: List[Any],
    inner_type: str,
    package: "UnPackage",
    struct_ref: str = "",
) -> Optional[bytes]:
    """Encode a list of values back to array element data."""
    if not elements:
        return b""

    buf = io.BytesIO()

    if inner_type.endswith(".ByteProperty"):
        for val in elements:
            buf.write(pack_byte(int(val)))
    elif inner_type.endswith(".IntProperty"):
        for val in elements:
            buf.write(pack_int(int(val)))
    elif inner_type.endswith(".FloatProperty"):
        for val in elements:
            buf.write(pack_float(float(val)))
    elif inner_type.endswith(".StrProperty"):
        for val in elements:
            s = _UnString(val)
            buf.write(s.serialize_to_bytes())
    elif inner_type.endswith(".ObjectProperty") or inner_type.endswith(
        ".ClassProperty"
    ):
        for val in elements:
            ref = package.link_item_ref(val) if val else 0
            write_index(buf, ref)
    elif inner_type.endswith(".NameProperty"):
        for val in elements:
            idx = package.link_name_index(val) if val else 0
            write_index(buf, idx)
    elif inner_type.endswith(".StructProperty"):
        if not struct_ref:
            return None
        for val in elements:
            if not isinstance(val, dict):
                return None
            if val.get("native") or _is_native_serialize_struct(struct_ref):
                encoded = _encode_struct(val, struct_ref, package)
            else:
                encoded = _encode_tagged_struct(val, struct_ref, package)
            if encoded is None:
                return None
            buf.write(encoded)
    else:
        return None

    return buf.getvalue()


# ===================================================================== #
#  Name index remapping in tagged property data
# ===================================================================== #


def _tagged_stream_is_unterminated(data: bytes, package: "UnPackage") -> bool:
    """Whether a struct value's tagged stream runs out before its ``None``.

    A struct value is serialised as a tagged property stream ending in a ``None``
    name index, but the *declared* size of the tag holding it can stop one index
    short of that terminator: ucc measures the size with a counting archive whose
    idea of the compact-index widths can differ from the real write by one byte.
    The engine reads the value as an unbounded stream and simply consumes the
    byte that follows, so the value is intact on disk — this reports the case so
    the reader can attribute that byte to the value instead of mistaking it for
    the end of the enclosing list.

    Args:
        data (bytes): The tag's raw value data.
        package (UnPackage): The package whose name table the indices refer into.

    Returns:
        bool: True when the stream ends exactly at a tag boundary with no
            terminator; False when it is terminated, empty, or unreadable
            (nothing is assumed about data that does not parse).
    """
    if not data:
        return False
    buf = io.BytesIO(data)
    size = len(data)
    try:
        while buf.tell() < size:
            idx = read_index(buf)
            if not 0 <= idx < len(package.names):
                return False
            if package.names[idx].name == "None":
                return False
            tag = UnPropertyTag()
            tag.name_index = idx
            tag.tag_name = package.names[idx]
            tag.parse(buf, package=package)
            if buf.tell() > size:
                return False
    except Exception:
        return False
    return True


def _remap_property_tag_data(
    tag: "UnPropertyTag", index_map: Dict[int, int], package: "UnPackage"
) -> None:
    """Remap name indices embedded in a tagged property's raw data.

    Must be called BEFORE the name table is rebuilt so *package* can still
    look up names by the old indices to identify ``"None"`` terminators.

    Handles:
      - ``NameProperty``: a single compact name index.
      - ``StructProperty``: nested tagged property block (recurse).
      - ``ArrayProperty``: count prefix followed by per-element data —
        when the inner type is ``StructProperty`` (tagged elements) the
        nested None-terminated blocks contain remappable indices.

    The pointer-to-name index inside the tag's own ``struct_name_index`` is
    re-linked separately via the serialize path; this function operates on
    the opaque ``property_data`` bytes only.
    """

    def _refresh_size_info() -> None:
        """Recompute ``tag.size`` and the size bits of ``tag.info`` from data length.

        The compact-index encoding used by name references can change byte
        length when the new index falls into a different value range, so
        rewritten ``property_data`` may differ in length from the original.
        Keep ``size`` and the size-class bits in ``info`` consistent so
        serialisation reads back correctly.
        """
        tag.size = len(tag.property_data)
        tag.info = (tag.info & 0x8F) | tag._encode_size_bits(tag.size)

    if not tag.property_data:
        return
    if tag.type == UnNameMap.NameProperty:
        buf = io.BytesIO(tag.property_data)
        old_idx = read_index(buf)
        new_idx = index_map.get(old_idx, old_idx)
        if new_idx != old_idx:
            out = io.BytesIO()
            write_index(out, new_idx)
            tag.property_data = out.getvalue()
            _refresh_size_info()
        return
    if tag.type == UnNameMap.DelegateProperty:
        # Delegate value = [object compact index][function-name compact index].
        # The object index is remapped by the object-ref pass; here we remap
        # the function-name index (else a name renumber rebinds the delegate to
        # the wrong function, e.g. a GUI dialog's OnClose handler).
        buf = io.BytesIO(tag.property_data)
        obj_ref = read_index(buf)
        old_name = read_index(buf)
        rest = buf.read()
        new_name = index_map.get(old_name, old_name)
        if new_name != old_name:
            out = io.BytesIO()
            write_index(out, obj_ref)
            write_index(out, new_name)
            out.write(rest)
            tag.property_data = out.getvalue()
            _refresh_size_info()
        return
    if tag.type == UnNameMap.StructProperty:
        # Tagged struct data uses None-terminated tag block format
        # for non-native structs; for natively-serialised structs
        # (Vector etc.) the data contains no name indices and the
        # remap is a no-op.
        struct_name = (
            tag.struct_name_entry.name
            if tag.struct_name_entry is not None
            else (
                package.names[tag.struct_name_index].name
                if 0 <= tag.struct_name_index < len(package.names)
                else ""
            )
        )
        if _is_native_serialize_struct(struct_name):
            return
        remapped = _remap_tagged_data_block(tag.property_data, index_map, package)
        if remapped != tag.property_data:
            tag.property_data = remapped
            _refresh_size_info()
        return
    if tag.type == UnNameMap.ArrayProperty:
        # Read the count prefix and re-emit verbatim; element data depends
        # on the inner property type.  When the inner is a tagged struct,
        # each element is a None-terminated block we must recurse into.
        buf = io.BytesIO(tag.property_data)
        try:
            count = read_index(buf)
        except Exception:
            return
        elem_data = buf.read()
        if count <= 0 or not elem_data:
            return

        tag_name = (
            tag.tag_name.name
            if tag.tag_name is not None
            else (
                package.names[tag.name_index].name
                if 0 <= tag.name_index < len(package.names)
                else ""
            )
        )
        inner_type, struct_ref = _find_array_inner_info(tag_name, package)
        if inner_type.endswith(".NameProperty"):
            # array<Name>: each element is a compact name index (e.g. the
            # ClassBlackList / FieldBlacklist / AllowedDllPackages a mod declares).
            # Remap every element so the entries survive a name renumber.
            out = io.BytesIO()
            write_index(out, count)
            ebuf = io.BytesIO(elem_data)
            try:
                for _ in range(count):
                    old = read_index(ebuf)
                    write_index(out, index_map.get(old, old))
                out.write(ebuf.read())
            except Exception:
                return
            if out.getvalue() != tag.property_data:
                tag.property_data = out.getvalue()
                _refresh_size_info()
            return
        if not inner_type.endswith(".StructProperty"):
            return
        if not struct_ref or _is_native_serialize_struct(struct_ref):
            return

        out = io.BytesIO()
        write_index(out, count)
        ebuf = io.BytesIO(elem_data)
        for _ in range(count):
            start = ebuf.tell()
            # Find the end of one tagged-struct block (None terminator)
            end = _scan_tagged_block_end(ebuf, package)
            if end is None:
                # Can't parse — write the remainder verbatim
                out.write(elem_data[start:])
                break
            block = elem_data[start:end]
            out.write(_remap_tagged_data_block(block, index_map, package))
            ebuf.seek(end)
        out.write(ebuf.read())
        if out.getvalue() != tag.property_data:
            tag.property_data = out.getvalue()
            _refresh_size_info()


def _scan_tagged_block_end(buf: BinaryIO, package: "UnPackage") -> Optional[int]:
    """Return the absolute stream position just past a None terminator.

    Used to slice one tagged-property block out of a stream of concatenated
    blocks (e.g. ArrayProperty<StructProperty> elements).  Returns ``None``
    on parse error.
    """
    try:
        while True:
            name_idx = read_index(buf)
            if not (0 <= name_idx < len(package.names)):
                return None
            if package.names[name_idx].name == "None":
                return buf.tell()
            info = read_byte(buf)
            prop_type = info & 0x0F
            if prop_type == UnNameMap.StructProperty:
                read_index(buf)  # struct name
            size_type = info & 0x70
            if size_type == 0x00:
                size = 1
            elif size_type == 0x10:
                size = 2
            elif size_type == 0x20:
                size = 4
            elif size_type == 0x30:
                size = 12
            elif size_type == 0x40:
                size = 16
            elif size_type == 0x50:
                size = read_byte(buf)
            elif size_type == 0x60:
                size = read_word(buf)
            elif size_type == 0x70:
                size = read_int(buf)
            else:
                size = 0
            if (info & 0x80) != 0 and prop_type != UnNameMap.BoolProperty:
                b = read_byte(buf)
                if (b & 0xC0) == 0x80:
                    read_byte(buf)
                elif (b & 0xC0) == 0xC0:
                    buf.read(3)
            buf.read(size)
    except Exception:
        return None


def _remap_tagged_data_block(
    data: bytes, index_map: Dict[int, int], package: "UnPackage"
) -> bytes:
    """Remap name indices inside a tagged property data block.

    Uses *package* (with old name table still intact) to identify ``"None"``
    terminators.  Recurses into nested StructProperty value data and
    NameProperty value data.

    When nested remapping changes a value's byte length (because the
    compact-index encoding for the new name index is shorter or longer),
    the enclosing tag's size field and the size-class bits in ``info``
    are recomputed so the rewritten stream remains parseable.

    A block that runs out before its terminator is accepted (and re-emitted the
    same way): see :attr:`UnPropertyTag.trailing_none_name` for why one exists.
    Bailing out there would leave every name index in the block stale.
    """
    try:
        buf = io.BytesIO(data)
        out = io.BytesIO()
        while True:
            if buf.tell() >= len(data):
                break
            name_idx = read_index(buf)
            new_name_idx = index_map.get(name_idx, name_idx)
            write_index(out, new_name_idx)

            # Terminator check uses the OLD name table since indices are
            # not yet remapped in *package*.
            if (
                0 <= name_idx < len(package.names)
                and package.names[name_idx].name == "None"
            ):
                break

            info = read_byte(buf)
            prop_type = info & 0x0F

            # Struct name index (consumed before size for StructProperty)
            new_struct_name_bytes = b""
            if prop_type == UnNameMap.StructProperty:
                sni = read_index(buf)
                sn_out = io.BytesIO()
                write_index(sn_out, index_map.get(sni, sni))
                new_struct_name_bytes = sn_out.getvalue()

            # Size
            size_type = info & 0x70
            size_field_bytes = b""
            if size_type == 0x00:
                size = 1
            elif size_type == 0x10:
                size = 2
            elif size_type == 0x20:
                size = 4
            elif size_type == 0x30:
                size = 12
            elif size_type == 0x40:
                size = 16
            elif size_type == 0x50:
                size_field_bytes = buf.read(1)
                size = size_field_bytes[0]
            elif size_type == 0x60:
                size_field_bytes = buf.read(2)
                size = int.from_bytes(size_field_bytes, "little")
            elif size_type == 0x70:
                size_field_bytes = buf.read(4)
                size = int.from_bytes(size_field_bytes, "little")
            else:
                size = 0

            # Array index bytes (preserved verbatim — array index isn't a name)
            array_bytes = b""
            if (info & 0x80) != 0 and prop_type != UnNameMap.BoolProperty:
                b0 = read_byte(buf)
                array_bytes = bytes([b0])
                if (b0 & 0x80) == 0:
                    pass
                elif (b0 & 0xC0) == 0x80:
                    array_bytes += bytes([read_byte(buf)])
                else:
                    array_bytes += bytes(buf.read(3))

            # Value data: remap nested names if applicable
            value_data = buf.read(size)
            if prop_type == UnNameMap.NameProperty and len(value_data) > 0:
                vbuf = io.BytesIO(value_data)
                old = read_index(vbuf)
                new = index_map.get(old, old)
                vout = io.BytesIO()
                write_index(vout, new)
                new_value = vout.getvalue()
            elif prop_type == UnNameMap.StructProperty and len(value_data) > 0:
                new_value = _remap_tagged_data_block(value_data, index_map, package)
            else:
                new_value = value_data

            new_size = len(new_value)
            if new_size == size:
                # Value length unchanged — re-emit the original info byte and
                # size field verbatim so a non-canonical size encoding (e.g. a
                # BoolProperty stored with an explicit 0-length size field) is
                # preserved exactly.  The struct-name index is still remapped.
                write_byte(out, info)
                if prop_type == UnNameMap.StructProperty:
                    out.write(new_struct_name_bytes)
                out.write(size_field_bytes)
            else:
                # Value length changed — recompute the size-class bits / field.
                new_size_type = (
                    UnPropertyTag._encode_size_bits(new_size) if new_size > 0 else 0x00
                )
                new_info = (info & 0x8F) | new_size_type
                write_byte(out, new_info)
                if prop_type == UnNameMap.StructProperty:
                    out.write(new_struct_name_bytes)
                if new_size_type == 0x50:
                    write_byte(out, new_size)
                elif new_size_type == 0x60:
                    write_word(out, new_size)
                elif new_size_type == 0x70:
                    write_int(out, new_size)

            out.write(array_bytes)
            out.write(new_value)

        return out.getvalue()
    except Exception:
        return data  # If remapping fails, return original


def remap_blob_export_names(export, index_map, package) -> bool:
    """Remap name indices embedded in an unparsed export's raw ``export_data``.

    Content objects whose class the tool does not model (``export.object is
    None`` — e.g. ``Engine.Texture``) are kept as an opaque ``export_data``
    blob whose leading section is a None-terminated tagged-property block
    (followed by native data such as texture mips).  Those embedded name
    indices — including the ``None`` terminator itself — are otherwise never
    remapped, so a name-table renumber (dedupe / prune) leaves them dangling
    and the engine reads past the block (serial-size mismatch on load).

    This rewrites the tagged-property block in place, preserving the trailing
    native data verbatim.  Objects carrying a serialised state frame
    (``HasStack``) are not handled — the blob does not start at the tagged
    block — and cause a ``False`` return so the caller can refuse to renumber.

    Args:
        export: The ``object is None`` export to process.
        index_map (Dict[int, int]): Old→new name-index map (a recording map
            with identity semantics may be passed to only collect indices).
        package ("UnPackage"): The owning package (old name table intact).

    Returns:
        bool: True when handled (or nothing to do); False when the blob cannot
        be safely walked (a ``HasStack`` object).
    """
    data = export.export_data
    if not data:
        return True
    if (export.flags & UnObjectFlags.HasStack) != 0:
        return False
    try:
        end = _scan_tagged_block_end(io.BytesIO(data), package)
    except Exception:
        end = None
    if end is None:
        return False
    block = data[:end]
    tail = data[end:]
    new_block = _remap_tagged_data_block(block, index_map, package)
    new_data = new_block + tail
    if new_data != data:
        export.export_data = new_data
        export.export_size = len(new_data)
    return True


def patch_texture_lazy_offsets(data: bytes, delta: int, package: "UnPackage") -> bytes:
    """Shift an ``Engine.Texture`` blob's ``TLazyArray`` mip ``SkipOffset``s by
    *delta* (the number of bytes the object moved in the output file).

    A texture the tool does not model is kept as a raw ``export_data`` blob.
    Its mip pixel data is stored in ``TLazyArray`` fields, each prefixed with
    an ABSOLUTE file offset (``SkipOffset``) pointing just past the array
    bytes.  When export removal / name pruning shrink earlier data
    the texture moves, so those offsets go stale and the engine reads past the
    object (``Serial size mismatch: Got N, Expected M``).  Shifting each by the
    object's move-delta restores them.

    Layout after the None-terminated tagged block::

        <mip count : compact index>
        per mip: <SkipOffset : int32> <Num : compact index> <Num data bytes>
                 <USize : int32> <VSize : int32> <UBits : byte> <VBits : byte>

    Returns the patched bytes, or the original unchanged when *delta* is 0 or
    the structure does not validate (the parse must consume exactly the blob).

    Args:
        data (bytes): The texture's raw ``export_data``.
        delta (int): ``new_file_offset - original_file_offset`` for the object.
        package ("UnPackage"): Owning package (to detect the ``None`` terminator).

    Returns:
        bytes: The offset-patched blob, or *data* unchanged if not applicable.
    """
    if delta == 0 or not data:
        return data
    try:
        buf = io.BytesIO(data)
        end = _scan_tagged_block_end(buf, package)
        if end is None:
            return data
        out = bytearray(data)
        buf.seek(end)
        mip_count = read_index(buf)
        if mip_count <= 0 or mip_count > 64:
            return data
        for _ in range(mip_count):
            skip_pos = buf.tell()
            old_skip = int.from_bytes(buf.read(4), "little", signed=True)
            num = read_index(buf)  # TLazyArray element (byte) count
            buf.read(num)  # pixel data
            buf.read(10)  # USize, VSize (int32) + UBits, VBits (byte)
            out[skip_pos : skip_pos + 4] = (old_skip + delta).to_bytes(
                4, "little", signed=True
            )
        if buf.tell() != len(data):
            return data  # structure mismatch — refuse to patch
        return bytes(out)
    except Exception:
        return data


# ===================================================================== #
#  Tagged property OBJECT-reference resolution (item-pointer backed)
# ===================================================================== #
#
# Name indices in tagged data are remapped by ``_remap_property_tag_data``
# above.  Object references (compact package-item indices embedded in
# ObjectProperty / ClassProperty / DelegateProperty values, plus the
# elements of object arrays and the members of nested tagged structs) are
# instead resolved to item pointers at load and re-derived at save, so the
# tables can be renumbered (e.g. ``remove_exports`` dropping exports) without
# leaving dangling indices.  ``_walk_tag_object_refs`` is the single
# structural pass shared by both directions: it rebuilds ``property_data``,
# calling ``fn(old_ref) -> new_ref`` once per object reference in a stable
# traversal order.


def _array_inner_has_objects(inner_type: str, struct_ref: str) -> bool:
    """Return True when an ArrayProperty's elements can contain object refs."""
    if inner_type.endswith((".ObjectProperty", ".ClassProperty")):
        return True
    if inner_type.endswith(".StructProperty"):
        return bool(struct_ref) and not _is_native_serialize_struct(struct_ref)
    return False


def _walk_array_object_refs(
    elem_data: bytes,
    count: int,
    inner_type: str,
    struct_ref: str,
    package: "UnPackage",
    fn,
) -> bytes:
    """Rebuild array element bytes, remapping object refs via *fn*.

    Handles object/class element arrays (each element is a compact object
    index) and tagged-struct element arrays (each element is a
    None-terminated tagged block, recursed into).
    """
    out = io.BytesIO()
    ebuf = io.BytesIO(elem_data)
    if inner_type.endswith((".ObjectProperty", ".ClassProperty")):
        for _ in range(count):
            old = read_index(ebuf)
            write_index(out, fn(old))
        out.write(ebuf.read())
        return out.getvalue()
    # StructProperty (non-native tagged) elements
    for _ in range(count):
        start = ebuf.tell()
        end = _scan_tagged_block_end(ebuf, package)
        if end is None:
            out.write(elem_data[start:])
            return out.getvalue()
        block = elem_data[start:end]
        out.write(_walk_tagged_block_object_refs(block, package, fn, struct_ref))
        ebuf.seek(end)
    out.write(ebuf.read())
    return out.getvalue()


def _walk_tagged_block_object_refs(
    data: bytes, package: "UnPackage", fn, parent_struct_name: str = ""
) -> bytes:
    """Rebuild a None-terminated tagged block, remapping object refs via *fn*.

    Mirrors the member-by-member parse of ``_remap_tagged_data_block`` but
    rewrites object references (in Object/Class/Delegate members, object
    arrays, and nested structs) rather than name indices.  Name indices and
    all non-object bytes are preserved verbatim; size-class bits are
    recomputed only where a value's byte length changes.
    """
    try:
        buf = io.BytesIO(data)
        out = io.BytesIO()
        while True:
            name_idx = read_index(buf)
            write_index(out, name_idx)
            if (
                0 <= name_idx < len(package.names)
                and package.names[name_idx].name == "None"
            ):
                break

            info = read_byte(buf)
            prop_type = info & 0x0F

            struct_name_bytes = b""
            member_struct_name = ""
            if prop_type == UnNameMap.StructProperty:
                sni = read_index(buf)
                sn_out = io.BytesIO()
                write_index(sn_out, sni)
                struct_name_bytes = sn_out.getvalue()
                if 0 <= sni < len(package.names):
                    member_struct_name = package.names[sni].name

            size_type = info & 0x70
            size_field_bytes = b""
            if size_type == 0x00:
                size = 1
            elif size_type == 0x10:
                size = 2
            elif size_type == 0x20:
                size = 4
            elif size_type == 0x30:
                size = 12
            elif size_type == 0x40:
                size = 16
            elif size_type == 0x50:
                size_field_bytes = buf.read(1)
                size = size_field_bytes[0]
            elif size_type == 0x60:
                size_field_bytes = buf.read(2)
                size = int.from_bytes(size_field_bytes, "little")
            elif size_type == 0x70:
                size_field_bytes = buf.read(4)
                size = int.from_bytes(size_field_bytes, "little")
            else:
                size = 0

            array_bytes = b""
            if (info & 0x80) != 0 and prop_type != UnNameMap.BoolProperty:
                b0 = read_byte(buf)
                array_bytes = bytes([b0])
                if (b0 & 0x80) == 0:
                    pass
                elif (b0 & 0xC0) == 0x80:
                    array_bytes += bytes([read_byte(buf)])
                else:
                    array_bytes += bytes(buf.read(3))

            member_name = (
                package.names[name_idx].name
                if 0 <= name_idx < len(package.names)
                else ""
            )
            value_data = buf.read(size)
            new_value = _walk_member_value_object_refs(
                value_data,
                prop_type,
                member_name,
                member_struct_name,
                parent_struct_name,
                package,
                fn,
            )

            new_size = len(new_value)
            if new_size == size:
                # Value length unchanged — re-emit the original header verbatim
                # so any non-canonical size encoding (e.g. a BoolProperty stored
                # with an explicit 0-length size field) is preserved exactly.
                write_byte(out, info)
                if prop_type == UnNameMap.StructProperty:
                    out.write(struct_name_bytes)
                out.write(size_field_bytes)
            else:
                new_size_type = (
                    UnPropertyTag._encode_size_bits(new_size) if new_size > 0 else 0x00
                )
                new_info = (info & 0x8F) | new_size_type
                write_byte(out, new_info)
                if prop_type == UnNameMap.StructProperty:
                    out.write(struct_name_bytes)
                if new_size_type == 0x50:
                    write_byte(out, new_size)
                elif new_size_type == 0x60:
                    write_word(out, new_size)
                elif new_size_type == 0x70:
                    write_int(out, new_size)
            out.write(array_bytes)
            out.write(new_value)

        return out.getvalue()
    except Exception:
        return data  # On parse failure, leave the block untouched


def _walk_member_value_object_refs(
    value_data: bytes,
    prop_type: int,
    member_name: str,
    member_struct_name: str,
    parent_struct_name: str,
    package: "UnPackage",
    fn,
) -> bytes:
    """Remap object refs inside one tagged-member value (see block walker)."""
    if not value_data:
        return value_data
    if prop_type in (UnNameMap.ObjectProperty, UnNameMap.ClassProperty):
        vbuf = io.BytesIO(value_data)
        old = read_index(vbuf)
        out = io.BytesIO()
        write_index(out, fn(old))
        out.write(vbuf.read())
        return out.getvalue()
    if prop_type == UnNameMap.DelegateProperty:
        vbuf = io.BytesIO(value_data)
        old = read_index(vbuf)
        out = io.BytesIO()
        write_index(out, fn(old))
        out.write(vbuf.read())  # trailing function-name index (a name, kept as-is)
        return out.getvalue()
    if prop_type == UnNameMap.StructProperty:
        if not member_struct_name or _is_native_serialize_struct(member_struct_name):
            return value_data
        return _walk_tagged_block_object_refs(
            value_data, package, fn, member_struct_name
        )
    if prop_type == UnNameMap.ArrayProperty:
        inner_type, struct_ref = _find_array_inner_info(
            member_name, package, parent_struct_name
        )
        if not _array_inner_has_objects(inner_type, struct_ref):
            return value_data
        abuf = io.BytesIO(value_data)
        count = read_index(abuf)
        rest = abuf.read()
        if count <= 0 or not rest:
            return value_data
        out = io.BytesIO()
        write_index(out, count)
        out.write(
            _walk_array_object_refs(rest, count, inner_type, struct_ref, package, fn)
        )
        return out.getvalue()
    return value_data


def _walk_tag_object_refs(tag: "UnPropertyTag", package: "UnPackage", fn):
    """Rebuild ``tag.property_data`` with each object ref passed through *fn*.

    *fn* is ``old_ref -> new_ref`` and is invoked once per object reference
    in a stable traversal order.  Returns the rebuilt bytes, or ``None`` when
    the tag carries no object references (caller leaves the data unchanged).
    """
    if not tag.property_data:
        return None
    t = tag.type
    if t in (
        UnNameMap.ObjectProperty,
        UnNameMap.ClassProperty,
        UnNameMap.DelegateProperty,
    ):
        buf = io.BytesIO(tag.property_data)
        old = read_index(buf)
        out = io.BytesIO()
        write_index(out, fn(old))
        out.write(buf.read())
        return out.getvalue()
    if t == UnNameMap.StructProperty:
        struct_name = (
            tag.struct_name_entry.name
            if tag.struct_name_entry is not None
            else (
                package.names[tag.struct_name_index].name
                if 0 <= tag.struct_name_index < len(package.names)
                else ""
            )
        )
        if not struct_name or _is_native_serialize_struct(struct_name):
            return None
        return _walk_tagged_block_object_refs(
            tag.property_data, package, fn, struct_name
        )
    if t == UnNameMap.ArrayProperty:
        tag_name = (
            tag.tag_name.name
            if tag.tag_name is not None
            else (
                package.names[tag.name_index].name
                if 0 <= tag.name_index < len(package.names)
                else ""
            )
        )
        inner_type, struct_ref = _find_array_inner_info(tag_name, package)
        if not _array_inner_has_objects(inner_type, struct_ref):
            return None
        buf = io.BytesIO(tag.property_data)
        count = read_index(buf)
        rest = buf.read()
        if count <= 0 or not rest:
            return None
        out = io.BytesIO()
        write_index(out, count)
        out.write(
            _walk_array_object_refs(rest, count, inner_type, struct_ref, package, fn)
        )
        return out.getvalue()
    return None


def _resolve_tag_object_refs(tag: "UnPropertyTag", package: "UnPackage") -> None:
    """Capture item pointers for every object ref in a tagged property.

    Stores ``tag._obj_ref_items`` (resolved :class:`UnPackageItem` list) and
    ``tag._obj_ref_olds`` (the original ref ints) in traversal order, leaving
    the bytes unchanged.  ``_link_tag_object_refs`` re-derives the indices.
    """
    items: List[Optional["UnPackageItem"]] = []
    olds: List[int] = []

    def collect(old: int) -> int:
        olds.append(old)
        items.append(resolve_item(package, old))
        return old

    try:
        _walk_tag_object_refs(tag, package, collect)
    except Exception:
        tag._obj_ref_items = None
        tag._obj_ref_olds = None
        return
    tag._obj_ref_items = items if items else None
    tag._obj_ref_olds = olds if olds else None


def _link_tag_object_refs(tag: "UnPropertyTag", package: "UnPackage") -> None:
    """Re-derive object-ref indices in a tagged property from item pointers.

    Idempotent and byte-preserving: when no ref actually changed value the
    data is left exactly as-is (so an unmodified round-trip is byte-identical);
    otherwise ``property_data`` is rebuilt and the tag's size / info bits are
    refreshed to match.
    """
    items = getattr(tag, "_obj_ref_items", None)
    if not items:
        return
    new_refs = [link_item(package, item) for item in items]
    if new_refs == getattr(tag, "_obj_ref_olds", None):
        return  # No renumbering affected this tag — keep bytes verbatim.
    it = iter(new_refs)

    def replace(_old: int) -> int:
        try:
            return next(it)
        except StopIteration:
            return _old

    try:
        new_data = _walk_tag_object_refs(tag, package, replace)
    except Exception:
        return
    if new_data is not None and new_data != tag.property_data:
        tag.property_data = new_data
        tag.size = len(tag.property_data)
        tag.info = (tag.info & 0x8F) | tag._encode_size_bits(tag.size)


# ===================================================================== #
#  Tagged property value codec dispatch (type int → property field class)
# ===================================================================== #

_PROPERTY_TYPE_MAP: Dict[int, Type["UnProperty"]] = {
    UnNameMap.ByteProperty: UnByteProperty,
    UnNameMap.IntProperty: UnIntProperty,
    UnNameMap.FloatProperty: UnFloatProperty,
    UnNameMap.ObjectProperty: UnObjectProperty,
    UnNameMap.NameProperty: UnNameProperty,
    UnNameMap.StrProperty: UnStrProperty,
    UnNameMap.ArrayProperty: UnArrayProperty,
    UnNameMap.DelegateProperty: UnDelegateProperty,
}

# Field class name → byte size (for simple fixed-size fields)
_FIELD_CLASS_SIZE: Dict[str, int] = {
    "Core.ByteProperty": 1,
    "Core.IntProperty": 4,
    "Core.BoolProperty": 4,  # UBOOL = INT
    "Core.FloatProperty": 4,
    "Core.ObjectProperty": 4,  # compact index (variable, but usually ≤4)
    "Core.NameProperty": 4,  # compact index (variable, but usually ≤4)
}


def _get_struct_fields(struct_name: str, package: "UnPackage") -> Optional[List[tuple]]:
    """Find a struct definition and return its fields as (name, class_name) pairs.

    Searches the package's own exports and all loaded dependency packages.
    Walks ``children`` / ``next_item`` via :func:`_resolve_via_ref` so it
    works both after ``resolve_objects()`` and during XML import (when
    only integer references are populated).  Returns ``None`` if the
    struct cannot be found.
    """
    bare = struct_name.lstrip("+-")

    def _find_in_pkg(pkg: "UnPackage") -> Optional["UnStruct"]:
        """Return the matching struct object in *pkg*, or None if absent.

        Args:
            pkg ("UnPackage"): The package to search.

        Returns:
            Optional["UnStruct"]: The matching struct object, or None.
        """
        for exp in pkg.exports:
            if (
                exp.object_name_string == bare
                or exp.object_name.name == bare.split(".")[-1]
            ):
                if isinstance(exp.object, UnStruct):
                    return exp.object
        return None

    home_pkg: "UnPackage" = package
    struct_obj = _find_in_pkg(package)
    if struct_obj is None:
        for dep_pkg in package.imported_packages.values():
            struct_obj = _find_in_pkg(dep_pkg)
            if struct_obj is not None:
                home_pkg = dep_pkg
                break

    if struct_obj is None:
        return None

    def _iter_struct_children(s: "UnStruct", owner_pkg: "UnPackage") -> List[tuple]:
        """Return a struct's direct children as (name, class_name) pairs.

        Args:
            s ("UnStruct"): The struct whose children to walk.
            owner_pkg ("UnPackage"): The package owning the struct.

        Returns:
            List[tuple]: A list of ``(field_name, class_name)`` pairs.
        """
        out: List[tuple] = []
        child = _resolve_via_ref(s.children, s.children_reference, owner_pkg)
        while child is not None:
            obj = child.object
            out.append((child.object_name.name, child.class_name_string))
            if isinstance(obj, UnField):
                child = _resolve_via_ref(obj.next_item, obj.next_reference, owner_pkg)
            else:
                break
        return out

    fields: List[tuple] = []
    # Walk super struct's fields first (inheritance)
    super_item = _resolve_via_ref(
        struct_obj.super_item, struct_obj.super_index, home_pkg
    )
    if super_item is not None and isinstance(super_item.object, UnStruct):
        fields.extend(_iter_struct_children(super_item.object, home_pkg))

    # Walk this struct's own fields
    fields.extend(_iter_struct_children(struct_obj, home_pkg))

    return fields if fields else None


# In UT2004's compatibility mode these structs use the raw sequential
# (native) serialisation path instead of the tagged-property format.
_NATIVE_SERIALIZE_STRUCT_NAMES = {"Vector", "Rotator", "Color"}


def _is_native_serialize_struct(struct_name: str) -> bool:
    """Return True if *struct_name* refers to a struct serialised natively.

    Only ``Vector``, ``Rotator`` and ``Color`` use the raw sequential
    layout; all others use the tagged-property format.
    """
    if not struct_name:
        return False
    bare = struct_name.lstrip("+-")
    short = bare.split(".")[-1]
    return short in _NATIVE_SERIALIZE_STRUCT_NAMES


def _find_nested_struct_ref(
    field_name: str, parent_struct_name: str, package: "UnPackage"
) -> str:
    """Find the struct type reference for a StructProperty field inside a struct."""
    bare = parent_struct_name.lstrip("+-")
    for pkg in [package] + list(package.imported_packages.values()):
        for exp in pkg.exports:
            if (
                exp.object_name_string == bare
                or exp.object_name.name == bare.split(".")[-1]
            ):
                if isinstance(exp.object, UnStruct):
                    # Walk children to find the field
                    child = exp.object.children
                    while child is not None:
                        obj = child.object
                        if child.object_name.name == field_name and isinstance(
                            obj, UnStructProperty
                        ):
                            si = obj.struct_item
                            if si is not None:
                                return si.object_name_string
                        child = obj.next_item if isinstance(obj, UnField) else None
    return ""


# Map from UProperty class name (suffix) to UnNameMap tagged-property type id.
_FIELD_CLASS_TO_TYPE: Dict[str, int] = {
    "ByteProperty": int(UnNameMap.ByteProperty),
    "IntProperty": int(UnNameMap.IntProperty),
    "BoolProperty": int(UnNameMap.BoolProperty),
    "FloatProperty": int(UnNameMap.FloatProperty),
    "ObjectProperty": int(UnNameMap.ObjectProperty),
    "ClassProperty": int(UnNameMap.ObjectProperty),  # subclass — same tagged type
    "NameProperty": int(UnNameMap.NameProperty),
    "StrProperty": int(UnNameMap.StrProperty),
    "ArrayProperty": int(UnNameMap.ArrayProperty),
    "StructProperty": int(UnNameMap.StructProperty),
    "FixedArrayProperty": int(UnNameMap.FixedArrayProperty),
}


def _infer_field_type(
    field_name: str, parent_struct_name: str, package: "UnPackage"
) -> Optional[int]:
    """Look up the tagged-property type id for a named field of a struct.

    Returns ``None`` if the struct or field cannot be resolved, or if the
    field's property class doesn't map to a known tagged type.  Used during
    XML import to recover the ``type`` attribute when it was omitted as
    redundant during export.
    """
    if not parent_struct_name or not field_name:
        return None
    fields = _get_struct_fields(parent_struct_name, package)
    if fields is None:
        return None
    for fname, fclass in fields:
        if fname != field_name:
            continue
        short = fclass.rsplit(".", 1)[-1]
        return _FIELD_CLASS_TO_TYPE.get(short)
    return None


def _calc_native_struct_size(
    struct_name: str, package: "UnPackage", _seen: Optional[set] = None
) -> Optional[int]:
    """Calculate the fixed byte size of a native struct from its fields.

    Returns ``None`` if any field is variable-length or cannot be resolved.
    """
    if _seen is None:
        _seen = set()
    bare = struct_name.lstrip("+-")
    if bare in _seen:
        return None  # circular
    _seen.add(bare)

    fields = _get_struct_fields(struct_name, package)
    if fields is None:
        return None
    size = 0
    for field_name, fc in fields:
        if fc.endswith(".ByteProperty"):
            size += 1
        elif fc.endswith((".IntProperty", ".FloatProperty", ".BoolProperty")):
            size += 4
        elif fc.endswith(".StructProperty"):
            nested_ref = _find_nested_struct_ref(field_name, struct_name, package)
            if nested_ref:
                nested_size = _calc_native_struct_size(nested_ref, package, _seen)
                if nested_size is not None:
                    size += nested_size
                    continue
            return None
        else:
            return None
    return size


def _decode_struct(
    data: bytes, struct_name: str, package: "UnPackage"
) -> Optional[Dict[str, Any]]:
    """Decode native struct data (raw sequential fields) into a dict.

    Returns ``None`` if *data* is shorter than the struct's declared
    layout — partial decoding cannot be round-tripped because the
    encoder always writes the full layout.  Hex fallback preserves the
    original truncated bytes verbatim.
    """
    fields = _get_struct_fields(struct_name, package)
    if fields is None:
        return None

    result: Dict[str, Any] = {}
    buf = io.BytesIO(data)

    for field_name, field_class in fields:
        remaining = len(data) - buf.tell()
        if remaining <= 0:
            # Truncated input — can't round-trip cleanly; fall back to hex.
            return None

        if field_class.endswith(".ByteProperty"):
            if remaining < 1:
                return None
            fd = buf.read(1)
            result[field_name] = str(fd[0])
        elif field_class.endswith(".IntProperty"):
            if remaining < 4:
                return None
            result[field_name] = str(unpack_int(buf.read(4)))
        elif field_class.endswith(".FloatProperty"):
            if remaining < 4:
                return None
            result[field_name] = format_float32(unpack_float(buf.read(4)))
        elif field_class.endswith(".BoolProperty"):
            if remaining < 4:
                return None
            result[field_name] = str(unpack_int(buf.read(4)))
        elif field_class.endswith(".StrProperty"):
            s = _UnString()
            s.parse(buf)
            result[field_name] = s.value
        elif field_class.endswith((".ObjectProperty", ".ClassProperty")):
            ref = read_index(buf)
            result[field_name] = package.resolve_item_ref(ref) if package else str(ref)
        elif field_class.endswith(".NameProperty"):
            idx = read_index(buf)
            result[field_name] = (
                package.resolve_name_index(idx) if package else str(idx)
            )
        elif field_class.endswith(".StructProperty"):
            # Find the nested struct's type from its property definition
            nested_struct_ref = _find_nested_struct_ref(
                field_name, struct_name, package
            )
            if nested_struct_ref:
                nested_size = _calc_native_struct_size(nested_struct_ref, package)
                if nested_size and nested_size > 0:
                    if remaining < nested_size:
                        return None
                    chunk = buf.read(nested_size)
                    nested = _decode_struct(chunk, nested_struct_ref, package)
                    if nested is not None:
                        result[field_name] = nested
                        continue
            return None  # can't decode nested struct
        else:
            return None  # unknown field type

    # Ensure no trailing bytes — full struct must be consumed cleanly
    if buf.tell() != len(data):
        return None

    return {"native": True, "fields": result}


def _decode_struct_streaming(
    buf: BinaryIO, struct_name: str, package: "UnPackage"
) -> Optional[Dict[str, Any]]:
    """Decode a native struct reading field-by-field from a stream (no pre-sized chunk)."""
    fields = _get_struct_fields(struct_name, package)
    if fields is None:
        return None

    result: Dict[str, Any] = {}
    for field_name, field_class in fields:
        if field_class.endswith(".ByteProperty"):
            result[field_name] = str(read_byte(buf))
        elif field_class.endswith(".IntProperty"):
            result[field_name] = str(unpack_int(buf.read(4)))
        elif field_class.endswith(".FloatProperty"):
            result[field_name] = format_float32(unpack_float(buf.read(4)))
        elif field_class.endswith(".BoolProperty"):
            result[field_name] = str(unpack_int(buf.read(4)))
        elif field_class.endswith(".StrProperty"):
            s = _UnString()
            s.parse(buf)
            result[field_name] = s.value
        elif field_class.endswith((".ObjectProperty", ".ClassProperty")):
            ref = read_index(buf)
            result[field_name] = package.resolve_item_ref(ref) if package else str(ref)
        elif field_class.endswith(".NameProperty"):
            idx = read_index(buf)
            result[field_name] = (
                package.resolve_name_index(idx) if package else str(idx)
            )
        elif field_class.endswith(".StructProperty"):
            nested_ref = _find_nested_struct_ref(field_name, struct_name, package)
            if nested_ref:
                nested = _decode_struct_streaming(buf, nested_ref, package)
                if nested is not None:
                    result[field_name] = nested
                    continue
            return None
        else:
            return None

    return {"native": True, "fields": result}


def _encode_struct(
    struct_dict: Dict[str, Any], struct_name: str, package: "UnPackage"
) -> Optional[bytes]:
    """Encode native struct data from field dict back to bytes."""
    fields_data = struct_dict.get("fields", {})
    fields = _get_struct_fields(struct_name, package)
    if fields is None:
        return None

    buf = io.BytesIO()
    for field_name, field_class in fields:
        value = fields_data.get(field_name, "0")
        if field_class.endswith(".ByteProperty"):
            buf.write(pack_byte(int(value)))
        elif field_class.endswith(".IntProperty"):
            buf.write(pack_int(int(value)))
        elif field_class.endswith(".FloatProperty"):
            buf.write(pack_float(float(value)))
        elif field_class.endswith(".BoolProperty"):
            buf.write(pack_int(int(value)))
        elif field_class.endswith(".StrProperty"):
            s = _UnString(value if value != "0" else "")
            s.serialize(buf)
        elif field_class.endswith((".ObjectProperty", ".ClassProperty")):
            ref = (
                package.link_item_ref(value)
                if package and value and value != "0"
                else 0
            )
            write_index(buf, ref)
        elif field_class.endswith(".NameProperty"):
            idx = (
                package.link_name_index(value)
                if package and value and value != "0"
                else 0
            )
            write_index(buf, idx)
        elif field_class.endswith(".StructProperty"):
            if isinstance(value, dict) and value.get("native", False):
                nested_ref = _find_nested_struct_ref(field_name, struct_name, package)
                if nested_ref:
                    encoded = _encode_struct(value, nested_ref, package)
                    if encoded is not None:
                        buf.write(encoded)
                        continue
            return None
        else:
            return None  # unknown field type
    return buf.getvalue()


def _decode_tagged_struct(
    data: bytes, struct_name: str, package: "UnPackage"
) -> Optional[Dict[str, Any]]:
    """Decode non-native struct data (tagged properties) into a dict.

    Expects *data* to contain exactly one tagged struct (no trailing bytes).
    *struct_name* scopes ArrayProperty inner-type lookups to the children
    of the named struct definition (matching how tagged-property
    serialisation walks the property link chain).
    """
    if not data or not package:
        return None
    try:
        buf = io.BytesIO(data)
        result = _decode_tagged_struct_streaming(
            buf, package, parent_struct_name=struct_name
        )
        if result is None:
            return None
        remaining = data[buf.tell() :]
        if remaining:
            return None
        return result
    except Exception:
        return None


def _decode_tagged_struct_streaming(
    buf: BinaryIO, package: "UnPackage", parent_struct_name: str = ""
) -> Optional[Dict[str, Any]]:
    """Decode one tagged struct from a stream, stopping at the None sentinel.

    Returns the decoded dict or ``None`` on failure.
    Does **not** check for trailing data — suitable for array element decoding.
    *parent_struct_name* scopes ArrayProperty inner-type lookups to the
    children of the named struct definition.
    """
    try:
        tags: List[Dict[str, Any]] = []
        none_index_str = ""
        while True:
            name_index = read_index(buf)
            if (
                0 <= name_index < len(package.names)
                and package.names[name_index].name == "None"
            ):
                resolved = package.resolve_name_index(name_index)
                if resolved != "None":
                    none_index_str = resolved
                break
            tag = UnPropertyTag()
            tag.name_index = name_index
            if 0 <= name_index < len(package.names):
                tag.tag_name = package.names[name_index]
            tag.parse(buf, package=package)
            tags.append(tag.to_dict(package, parent_struct_name=parent_struct_name))
        return {"native": False, "tags": tags, "none_index": none_index_str}
    except Exception:
        return None


def _encode_tagged_struct(
    struct_dict: Dict[str, Any], struct_name: str, package: "UnPackage"
) -> Optional[bytes]:
    """Encode non-native struct data from tagged property list back to bytes.

    When a tag dict is missing its ``type`` attribute (omitted during XML
    export because it's redundant given the parent struct definition), the
    type is recovered by looking up the field by name in *struct_name*'s
    children chain via :func:`_infer_field_type`.
    """
    tags_list = struct_dict.get("tags", [])
    none_index_str = struct_dict.get("none_index", "")
    try:
        buf = io.BytesIO()
        for tag_dict in tags_list:
            tag_dict = _fill_implicit_field_attrs(tag_dict, struct_name, package)
            tag = UnPropertyTag()
            tag.from_dict(tag_dict, package, parent_struct_name=struct_name)
            write_index(buf, tag.name_index)
            tag.serialize(buf, package=package)
        if none_index_str:
            none_idx = package.link_name_index(none_index_str)
        else:
            none_idx = 0
            for i, n in enumerate(package.names):
                if n.name == "None":
                    none_idx = i
                    break
        write_index(buf, none_idx)
        return buf.getvalue()
    except Exception:
        return None


def _fill_implicit_field_attrs(
    tag_dict: Dict[str, Any], parent_struct_name: str, package: "UnPackage"
) -> Dict[str, Any]:
    """Return *tag_dict* with implicit attributes filled in from the struct def.

    During XML export the ``type`` attribute is omitted from ``<Field>``
    elements because it is uniquely determined by the field's name in the
    parent struct's children list.  This helper reconstructs it on import.
    Returns a new dict (does not mutate *tag_dict*).
    """
    if "type" in tag_dict:
        return tag_dict
    name = tag_dict.get("name", "")
    inferred = _infer_field_type(name, parent_struct_name, package)
    if inferred is None:
        return tag_dict
    new_dict = dict(tag_dict)
    new_dict["type"] = UnPropertyTag._type_to_name(inferred)
    return new_dict


def _decode_struct_data(
    data: bytes, struct_name: str, package: "UnPackage"
) -> Optional[Dict[str, Any]]:
    """Decode struct property data.

    Dispatches based on struct name:
      - ``Vector`` / ``Rotator`` / ``Color`` use the native sequential layout
      - All others use the tagged-property layout (None-terminated)
    """
    if not data or not package:
        return None
    if _is_native_serialize_struct(struct_name):
        return _decode_struct(data, struct_name, package)
    return _decode_tagged_struct(data, struct_name, package)


def _encode_struct_data(
    struct_dict: Dict[str, Any], struct_name: str, package: "UnPackage"
) -> Optional[bytes]:
    """Encode struct data, choosing native or tagged format.

    Dispatches based on the ``native`` flag in *struct_dict* (set by the
    decoder); for safety also forces the native path when *struct_name*
    refers to one of the natively-serialised structs (Vector/Rotator/Color).
    """
    if not struct_dict or not package:
        return None
    if struct_dict.get("native", False) or _is_native_serialize_struct(struct_name):
        return _encode_struct(struct_dict, struct_name, package)
    return _encode_tagged_struct(struct_dict, struct_name, package)


# ===================================================================== #
#  Content object types (textures, sounds, materials)
# ===================================================================== #
#
# These classes model the on-disk layout of engine *content* exports so that
# their references are resolved through the normal object/name pipeline
# instead of scanning their raw bytes.  Every one begins with the standard
# object payload (optional state frame + tagged properties + ``None``
# terminator); some carry class-specific native data after it.
#
# Because they are fully parsed:
#   * object references embedded in their tagged properties are resolved to
#     item pointers and re-linked on save by the base :class:`UnObject`, so a
#     renumbering ``link()`` keeps them valid with no blob remapping;
#   * name indices are re-derived from name pointers at write time, so name
#     pruning/deduplication needs no blob remapping either;
#   * lazy-array skip offsets (absolute file positions) are recomputed from the
#     object's write position, so the data stays valid wherever the object
#     lands in the output file.


class _UnContentObject(UnObject):
    """Base for fully-modelled content exports.

    Reads the standard object payload via :class:`UnObject` and then an
    optional class-specific native tail (overridden by subclasses).  Any bytes
    left after the modelled tail are preserved verbatim so an unmodelled trailer
    can never corrupt a round-trip.
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise the content object with an empty native trailer.

        Args:
            export ("UnExport"): The export entry that owns this object.
        """
        super().__init__(export)
        self._trailing: bytes = b""

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the class-specific native tail (no-op by default).

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.
        """
        return

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the class-specific native tail (no-op by default).

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream (for computing absolute skip offsets).
        """
        return

    def _parse(self, reader: BinaryIO) -> None:
        """Parse the object payload, native tail, and any trailing bytes.

        Args:
            reader (BinaryIO): Binary stream positioned at the object data.
        """
        super()._parse(reader)
        self._parse_tail(reader)
        self._trailing = reader.read()

    def _serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the object payload, native tail, and trailing bytes.

        Args:
            writer (BinaryIO): Binary stream to write to.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        super()._serialize(writer, stream_position)
        self._serialize_tail(writer, stream_position)
        if self._trailing:
            writer.write(self._trailing)

    def to_dict(self) -> Dict[str, Any]:
        """Return the object's dict representation including any trailer.

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        if self._trailing:
            d["trailing"] = bytes_to_hex(self._trailing)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the object from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        trailing = data.get("trailing")
        self._trailing = hex_to_bytes(trailing) if trailing else b""

    def dump(self) -> str:
        """Return a one-line summary of the content object.

        Returns:
            str: A description with the class and tagged-property count.
        """
        return (
            f"{self.__class__.__name__}({self.export.object_name_string}, "
            f"{len(self.tagged_properties)} tagged props)"
        )


class UnConstantColor(_UnContentObject):
    """A constant-colour material.  Pure tagged-property payload."""


class UnCombiner(_UnContentObject):
    """A combiner material.  Pure tagged-property payload."""


class UnShader(_UnContentObject):
    """A shader material.  Pure tagged-property payload."""


class UnFinalBlend(_UnContentObject):
    """A final-blend material modifier.  Pure tagged-property payload."""


class UnTexture(_UnContentObject):
    """A bitmap texture.

    After the standard object payload comes a mipmap array::

        <mip count : compact index>
        per mip: <skip offset : int32> <byte count : compact index> <pixels>
                 <USize : int32> <VSize : int32> <UBits : byte> <VBits : byte>

    The per-mip *skip offset* is the ABSOLUTE file position immediately after
    that mip's pixel bytes; it is recomputed from the object's write position
    so it stays correct wherever the texture lands in the output file.  The
    bulk pixel data is stored in a sidecar ``.bin`` file on XML export.
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise the texture with an empty mip list.

        Args:
            export ("UnExport"): The export entry that owns this texture.
        """
        super().__init__(export)
        # Each mip: {"data": bytes, "usize": int, "vsize": int,
        #            "ubits": int, "vbits": int}
        self.mips: List[Dict[str, Any]] = []

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the mipmap array.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.
        """
        mip_count = read_index(reader)
        self.mips = []
        for _ in range(mip_count):
            read_int(reader)  # skip offset (absolute; recomputed on write)
            num = read_index(reader)
            data = reader.read(num)
            usize = read_int(reader)
            vsize = read_int(reader)
            ubits = read_byte(reader)
            vbits = read_byte(reader)
            self.mips.append(
                {
                    "data": data,
                    "usize": usize,
                    "vsize": vsize,
                    "ubits": ubits,
                    "vbits": vbits,
                }
            )

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the mipmap array, recomputing absolute skip offsets.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        write_index(writer, len(self.mips))
        for mip in self.mips:
            skip_pos = writer.tell()
            write_int(writer, 0)  # placeholder skip offset
            data = mip["data"]
            write_index(writer, len(data))
            writer.write(data)
            end_abs = stream_position + writer.tell()  # just past the pixels
            write_int(writer, mip["usize"])
            write_int(writer, mip["vsize"])
            write_byte(writer, mip["ubits"])
            write_byte(writer, mip["vbits"])
            resume = writer.tell()
            writer.seek(skip_pos)
            write_int(writer, end_abs)
            writer.seek(resume)

    def to_dict(self) -> Dict[str, Any]:
        """Return the texture's dict representation (with hex mip pixels).

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        d["mips"] = [
            {
                "usize": m["usize"],
                "vsize": m["vsize"],
                "ubits": m["ubits"],
                "vbits": m["vbits"],
                "data": bytes_to_hex(m["data"]),
            }
            for m in self.mips
        ]
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the texture from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        self.mips = []
        for m in data.get("mips", []):
            self.mips.append(
                {
                    "usize": int(m.get("usize", 0)),
                    "vsize": int(m.get("vsize", 0)),
                    "ubits": int(m.get("ubits", 0)),
                    "vbits": int(m.get("vbits", 0)),
                    "data": hex_to_bytes(m.get("data", "")),
                }
            )

    def export_xml(self, obj_dict: Dict[str, Any], output_dir: str) -> None:
        """Move bulk mip pixels into a sidecar ``.bin`` file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            output_dir (str): The directory to write the sidecar file into.
        """
        mips = obj_dict.get("mips", [])
        blob = bytearray()
        for m in mips:
            raw = hex_to_bytes(m.pop("data", ""))
            m["length"] = len(raw)
            blob += raw
        bin_filename = self.export.object_name_string + ".bin"
        bin_subdir = os.path.join(output_dir, "UnTexture")
        os.makedirs(bin_subdir, exist_ok=True)
        with open(os.path.join(bin_subdir, bin_filename), "wb") as f:
            f.write(bytes(blob))
        obj_dict["mip_data_file"] = bin_filename

    def import_xml(self, obj_dict: Dict[str, Any], input_dir: str) -> None:
        """Read bulk mip pixels back from the sidecar ``.bin`` file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            input_dir (str): The directory to read the sidecar file from.
        """
        bin_filename = obj_dict.pop("mip_data_file", "")
        if not bin_filename:
            return
        with open(os.path.join(input_dir, "UnTexture", bin_filename), "rb") as f:
            blob = f.read()
        pos = 0
        for m in obj_dict.get("mips", []):
            length = int(m.pop("length", 0))
            m["data"] = bytes_to_hex(blob[pos : pos + length])
            pos += length


class UnSound(_UnContentObject):
    """A sound effect.

    After the standard object payload comes::

        <file type : name index> <likelihood : float32>
        <skip offset : int32> <byte count : compact index> <audio bytes>

    The *skip offset* is the ABSOLUTE file position immediately after the audio
    bytes and is recomputed on write.  ``file_type`` is held as a name pointer
    so it survives name-table pruning/renumbering.  The bulk audio is stored in
    a sidecar ``.bin`` file on XML export.
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise the sound with empty data.

        Args:
            export ("UnExport"): The export entry that owns this sound.
        """
        super().__init__(export)
        self.file_type: Optional["UnName"] = None
        self.likelihood: float = 0.0
        self.data: bytes = b""

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the file type, likelihood, and audio data.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.
        """
        pkg = self.export.package
        ft_index = read_index(reader)
        self.file_type = pkg.names[ft_index] if 0 <= ft_index < len(pkg.names) else None
        self.likelihood = read_float(reader)
        read_int(reader)  # skip offset (absolute; recomputed on write)
        num = read_index(reader)
        self.data = reader.read(num)

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the file type, likelihood, and audio data.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        pkg = self.export.package
        write_index(writer, pkg.name_index(self.file_type) if self.file_type else 0)
        write_float(writer, self.likelihood)
        skip_pos = writer.tell()
        write_int(writer, 0)  # placeholder skip offset
        write_index(writer, len(self.data))
        writer.write(self.data)
        end_abs = stream_position + writer.tell()
        resume = writer.tell()
        writer.seek(skip_pos)
        write_int(writer, end_abs)
        writer.seek(resume)

    def deduplicate_names(self) -> None:
        """Re-resolve the file-type name pointer after a table rebuild."""
        super().deduplicate_names()
        if self.file_type is not None:
            self.file_type = self.export.package.find_name(self.file_type.name)

    def to_dict(self) -> Dict[str, Any]:
        """Return the sound's dict representation (with hex audio data).

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        pkg = self.export.package
        if self.file_type is not None:
            d["file_type"] = pkg.resolve_name_index(pkg.name_index(self.file_type))
        d["likelihood"] = self.likelihood
        d["data"] = bytes_to_hex(self.data)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the sound from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        pkg = self.export.package
        ft = data.get("file_type")
        if ft is not None:
            idx = self._link_name_index(str(ft))
            self.file_type = pkg.names[idx] if 0 <= idx < len(pkg.names) else None
        else:
            self.file_type = None
        self.likelihood = float(data.get("likelihood", 0.0))
        self.data = hex_to_bytes(data.get("data", ""))

    def export_xml(self, obj_dict: Dict[str, Any], output_dir: str) -> None:
        """Move bulk audio into a sidecar ``.bin`` file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            output_dir (str): The directory to write the sidecar file into.
        """
        raw = hex_to_bytes(obj_dict.pop("data", ""))
        bin_filename = self.export.object_name_string + ".bin"
        bin_subdir = os.path.join(output_dir, "UnSound")
        os.makedirs(bin_subdir, exist_ok=True)
        with open(os.path.join(bin_subdir, bin_filename), "wb") as f:
            f.write(raw)
        obj_dict["data_file"] = bin_filename

    def import_xml(self, obj_dict: Dict[str, Any], input_dir: str) -> None:
        """Read bulk audio back from the sidecar ``.bin`` file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            input_dir (str): The directory to read the sidecar file from.
        """
        bin_filename = obj_dict.pop("data_file", "")
        if not bin_filename:
            return
        with open(os.path.join(input_dir, "UnSound", bin_filename), "rb") as f:
            obj_dict["data"] = bytes_to_hex(f.read())


# ===================================================================== #
#  Mesh resources — shared value/array codecs
# ===================================================================== #
#
# The mesh classes below share a small vocabulary of building blocks:
#
#   * fixed-shape float bundles (vector, plane, rotator) and the byte-tagged
#     bounding box, held as plain dicts of named components;
#   * :class:`_UnRawArray`, a counted native array whose elements carry no
#     package references — the element bytes are kept verbatim, so the array
#     re-serialises byte-for-byte and its bulk can be moved into a sidecar
#     ``.bin`` file on XML export;
#   * :class:`_UnLazyArray`, the same thing prefixed with the ABSOLUTE file
#     offset of its end, which is recomputed from the object's write position;
#   * :class:`_UnObjRef` / :class:`_UnNameRef`, single object/name references
#     kept as resolved pointers so table renumbering and name pruning can
#     re-derive their indices.
#
# Structs that contain references expose ``_object_refs()`` / ``_name_refs()``
# generators so the owning object's ``resolve`` / ``link`` /
# ``deduplicate_names`` passes can walk them without repeating the layout.

_VECTOR_KEYS = ("x", "y", "z")
_PLANE_KEYS = ("x", "y", "z", "w")
_ROTATOR_KEYS = ("pitch", "yaw", "roll")
_COLOR_KEYS = ("r", "g", "b", "a")

# Key holding a raw array's element bytes in its dict form, and the key that
# replaces it once ``_UnBulkSidecar`` has moved those bytes into a ``.bin``.
_BULK_KEY = "bulk"
_BULK_LENGTH_KEY = "bulk_length"


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a dict/XML value to an int, tolerating absent or empty text.

    Args:
        value (Any): The value to coerce.
        default (int): The value to use when *value* is absent or empty.

    Returns:
        int: The coerced integer.
    """
    if value is None or value == "":
        return default
    return int(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a dict/XML value to a float, tolerating absent or empty text.

    Args:
        value (Any): The value to coerce.
        default (float): The value to use when *value* is absent or empty.

    Returns:
        float: The coerced float.
    """
    if value is None or value == "":
        return default
    return float(value)


def _as_list(value: Any) -> List[Any]:
    """Coerce a dict/XML value to a list, tolerating an absent or empty entry.

    An empty list has no child elements in the XML transport, so it comes back
    as empty text rather than a list.

    Args:
        value (Any): The value to coerce.

    Returns:
        List[Any]: The value as a list, or an empty list.
    """
    return value if isinstance(value, list) else []


def _read_count(reader: BinaryIO) -> int:
    """Read an array element count, rejecting a negative value.

    Args:
        reader (BinaryIO): Stream positioned at the count.

    Returns:
        int: The element count.

    Raises:
        ValueError: When the encoded count is negative.
    """
    count = read_index(reader)
    if count < 0:
        raise ValueError(f"negative array element count {count}")
    return count


def _read_exact(reader: BinaryIO, size: int) -> bytes:
    """Read exactly *size* bytes, refusing a short read.

    Args:
        reader (BinaryIO): Stream to read from.
        size (int): Number of bytes required.

    Returns:
        bytes: The bytes read.

    Raises:
        ValueError: When fewer than *size* bytes are available.
    """
    data = reader.read(size)
    if len(data) != size:
        raise ValueError(f"truncated payload: wanted {size} bytes, got {len(data)}")
    return data


def _read_float_bundle(reader: BinaryIO, keys) -> Dict[str, float]:
    """Read one float32 per key, in key order.

    Args:
        reader (BinaryIO): Stream positioned at the first component.
        keys: The component names, in stream order.

    Returns:
        Dict[str, float]: The named components.
    """
    return {key: read_float(reader) for key in keys}


def _write_float_bundle(writer: BinaryIO, values: Any, keys) -> None:
    """Write one float32 per key, in key order.

    Args:
        writer (BinaryIO): Stream to write to.
        values (Any): Mapping of component name to value (missing = 0.0).
        keys: The component names, in stream order.
    """
    src = values if isinstance(values, dict) else {}
    for key in keys:
        write_float(writer, _as_float(src.get(key)))


def _float_bundle_to_dict(values: Dict[str, float]) -> Dict[str, str]:
    """Render a float bundle as shortest-round-tripping decimal literals.

    Args:
        values (Dict[str, float]): The named components.

    Returns:
        Dict[str, str]: The components as float32 literals.
    """
    return {key: format_float32(value) for key, value in values.items()}


def _float_bundle_from_dict(data: Any, keys) -> Dict[str, float]:
    """Rebuild a float bundle from its dict representation.

    Args:
        data (Any): The dict representation (or anything else, treated as empty).
        keys: The component names, in stream order.

    Returns:
        Dict[str, float]: The named components.
    """
    src = data if isinstance(data, dict) else {}
    return {key: _as_float(src.get(key)) for key in keys}


def _read_int_bundle(reader: BinaryIO, keys) -> Dict[str, int]:
    """Read one int32 per key, in key order.

    Args:
        reader (BinaryIO): Stream positioned at the first component.
        keys: The component names, in stream order.

    Returns:
        Dict[str, int]: The named components.
    """
    return {key: read_int(reader) for key in keys}


def _write_int_bundle(writer: BinaryIO, values: Any, keys) -> None:
    """Write one int32 per key, in key order.

    Args:
        writer (BinaryIO): Stream to write to.
        values (Any): Mapping of component name to value (missing = 0).
        keys: The component names, in stream order.
    """
    src = values if isinstance(values, dict) else {}
    for key in keys:
        write_int(writer, _as_int(src.get(key)))


def _int_bundle_from_dict(data: Any, keys) -> Dict[str, int]:
    """Rebuild an int bundle from its dict representation.

    Args:
        data (Any): The dict representation (or anything else, treated as empty).
        keys: The component names, in stream order.

    Returns:
        Dict[str, int]: The named components.
    """
    src = data if isinstance(data, dict) else {}
    return {key: _as_int(src.get(key)) for key in keys}


def _read_byte_bundle(reader: BinaryIO, keys) -> Dict[str, int]:
    """Read one byte per key, in key order.

    Args:
        reader (BinaryIO): Stream positioned at the first component.
        keys: The component names, in stream order.

    Returns:
        Dict[str, int]: The named components.
    """
    return {key: read_byte(reader) for key in keys}


def _write_byte_bundle(writer: BinaryIO, values: Any, keys) -> None:
    """Write one byte per key, in key order.

    Args:
        writer (BinaryIO): Stream to write to.
        values (Any): Mapping of component name to value (missing = 0).
        keys: The component names, in stream order.
    """
    src = values if isinstance(values, dict) else {}
    for key in keys:
        write_byte(writer, _as_int(src.get(key)))


def _empty_box() -> Dict[str, Any]:
    """Return a zeroed, invalid bounding box.

    Returns:
        Dict[str, Any]: A bounding box with zero extents and ``is_valid`` 0.
    """
    return {
        "min": dict.fromkeys(_VECTOR_KEYS, 0.0),
        "max": dict.fromkeys(_VECTOR_KEYS, 0.0),
        "is_valid": 0,
    }


def _read_box(reader: BinaryIO) -> Dict[str, Any]:
    """Read a bounding box: two vectors plus a validity byte.

    Args:
        reader (BinaryIO): Stream positioned at the box.

    Returns:
        Dict[str, Any]: The parsed bounding box.
    """
    return {
        "min": _read_float_bundle(reader, _VECTOR_KEYS),
        "max": _read_float_bundle(reader, _VECTOR_KEYS),
        "is_valid": read_byte(reader),
    }


def _write_box(writer: BinaryIO, box: Any) -> None:
    """Write a bounding box: two vectors plus a validity byte.

    Args:
        writer (BinaryIO): Stream to write to.
        box (Any): The bounding box dict.
    """
    src = box if isinstance(box, dict) else {}
    _write_float_bundle(writer, src.get("min"), _VECTOR_KEYS)
    _write_float_bundle(writer, src.get("max"), _VECTOR_KEYS)
    write_byte(writer, _as_int(src.get("is_valid")))


def _box_to_dict(box: Dict[str, Any]) -> Dict[str, Any]:
    """Render a bounding box for the dict/XML transport.

    Args:
        box (Dict[str, Any]): The bounding box to render.

    Returns:
        Dict[str, Any]: The transport representation.
    """
    return {
        "min": _float_bundle_to_dict(box["min"]),
        "max": _float_bundle_to_dict(box["max"]),
        "is_valid": box["is_valid"],
    }


def _box_from_dict(data: Any) -> Dict[str, Any]:
    """Rebuild a bounding box from its dict representation.

    Args:
        data (Any): The dict representation.

    Returns:
        Dict[str, Any]: The parsed bounding box.
    """
    src = data if isinstance(data, dict) else {}
    return {
        "min": _float_bundle_from_dict(src.get("min"), _VECTOR_KEYS),
        "max": _float_bundle_from_dict(src.get("max"), _VECTOR_KEYS),
        "is_valid": _as_int(src.get("is_valid")),
    }


class _UnObjRef:
    """A package object reference: a compact index plus its resolved item.

    The item pointer is what survives table renumbering; the index is
    re-derived from it on save (mirroring :class:`UnField`'s super/next refs).
    """

    __slots__ = ("index", "item")

    def __init__(self) -> None:
        """Initialise an empty (``None``) reference."""
        self.index: int = 0
        self.item: Optional["UnPackageItem"] = None

    def parse(self, reader: BinaryIO) -> None:
        """Read the reference index from the stream.

        Args:
            reader (BinaryIO): Stream positioned at the compact index.
        """
        self.index = read_index(reader)

    def serialize(self, writer: BinaryIO) -> None:
        """Write the reference index to the stream.

        Args:
            writer (BinaryIO): Stream to write to.
        """
        write_index(writer, self.index)

    def resolve(self, package: "UnPackage") -> None:
        """Resolve the index into an item pointer.

        Args:
            package ("UnPackage"): The owning package.
        """
        self.item = resolve_item(package, self.index)

    def link(self, package: "UnPackage") -> None:
        """Re-derive the index from the item pointer.

        Args:
            package ("UnPackage"): The owning package.
        """
        self.index = link_item(package, self.item)

    def to_str(self, owner: "UnObject") -> str:
        """Return the reference as a prefixed name string.

        Args:
            owner ("UnObject"): The object that owns this reference.

        Returns:
            str: The prefixed name string.
        """
        return owner._resolve_object_ref(self.index)

    def from_str(self, owner: "UnObject", value: Any) -> None:
        """Populate the reference from a prefixed name string.

        Args:
            owner ("UnObject"): The object that owns this reference.
            value (Any): The prefixed name string.
        """
        self.index = owner._link_object_ref(str(value or ""))


class _UnNameRef:
    """A name-table reference held as a pointer to its table entry.

    Storing the entry (rather than an index) keeps the reference valid across
    name-table rebuilds; the index is re-derived on save.
    """

    __slots__ = ("entry",)

    def __init__(self) -> None:
        """Initialise an unset reference."""
        self.entry: Optional["UnName"] = None

    def parse(self, reader: BinaryIO, package: "UnPackage") -> None:
        """Read the name index and capture the table entry.

        Args:
            reader (BinaryIO): Stream positioned at the compact index.
            package ("UnPackage"): The owning package.
        """
        index = read_index(reader)
        self.entry = package.names[index] if 0 <= index < len(package.names) else None

    def serialize(self, writer: BinaryIO, package: "UnPackage") -> None:
        """Write the name index of the referenced entry.

        Args:
            writer (BinaryIO): Stream to write to.
            package ("UnPackage"): The owning package.
        """
        write_index(writer, package.name_index(self.entry) if self.entry else 0)

    def deduplicate(self, package: "UnPackage") -> None:
        """Re-resolve the entry after the name table was rebuilt.

        Args:
            package ("UnPackage"): The owning package.
        """
        if self.entry is not None:
            self.entry = package.find_name(self.entry.name)

    def to_str(self, owner: "UnObject") -> str:
        """Return the reference as a ``Name`` / ``Name@N`` string.

        Args:
            owner ("UnObject"): The object that owns this reference.

        Returns:
            str: The resolved name string.
        """
        if self.entry is None:
            return ""
        package = owner.export.package
        return package.resolve_name_index(package.name_index(self.entry))

    def from_str(self, owner: "UnObject", value: Any) -> None:
        """Populate the reference from a ``Name`` / ``Name@N`` string.

        Args:
            owner ("UnObject"): The object that owns this reference.
            value (Any): The name string.
        """
        text = str(value or "")
        if not text:
            self.entry = None
            return
        package = owner.export.package
        index = owner._link_name_index(text)
        self.entry = package.names[index] if 0 <= index < len(package.names) else None


class _UnRawArray:
    """A counted native array whose elements hold no package references.

    Only the element count is structural; the element bytes are kept verbatim
    so the array re-serialises byte-for-byte and its bulk can be offloaded to a
    sidecar ``.bin`` file on XML export.  With a fixed *elem_size* the count is
    implied by the payload length; subclasses with a variable element size
    (see :class:`_UnStaticMeshTriangleArray`) pass ``0`` and keep the count.
    """

    def __init__(self, elem_size: int) -> None:
        """Initialise an empty array.

        Args:
            elem_size (int): Bytes per element, or 0 when it varies.
        """
        self.elem_size = elem_size
        self.count: int = 0
        self.data: bytes = b""

    def _read_elements(self, reader: BinaryIO, count: int) -> bytes:
        """Read the element bytes for *count* elements.

        Args:
            reader (BinaryIO): Stream positioned at the first element.
            count (int): Number of elements to read.

        Returns:
            bytes: The element bytes.
        """
        return _read_exact(reader, count * self.elem_size)

    def parse(self, reader: BinaryIO) -> None:
        """Read the element count and the element bytes.

        Args:
            reader (BinaryIO): Stream positioned at the count.
        """
        self.count = _read_count(reader)
        self.data = self._read_elements(reader, self.count)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Write the element count followed by the element bytes.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object in
                the output stream (unused here; see :class:`_UnLazyArray`).
        """
        write_index(writer, self.count)
        writer.write(self.data)

    def to_dict(self) -> Dict[str, Any]:
        """Return the array's dict representation.

        ``count`` is informational — it is re-derived from the payload on
        import whenever the element size is fixed.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {"count": self.count, _BULK_KEY: bytes_to_hex(self.data)}

    def from_dict(self, data: Any) -> None:
        """Populate the array from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.data = hex_to_bytes(str(src.get(_BULK_KEY, "")))
        if self.elem_size:
            self.count = len(self.data) // self.elem_size
        else:
            self.count = _as_int(src.get("count"))


class _UnLazyArray(_UnRawArray):
    """A native array prefixed with the absolute file offset of its end.

    The offset points at the first byte past the element data.  It is
    recomputed from the owning object's write position so it stays correct
    wherever the object lands in the output file.

    ``end_offset`` and ``end_position`` keep what the parse actually read — the
    stored absolute offset, and the payload-relative position the elements
    ended at.  A well-formed array satisfies
    ``end_offset == <object's file offset> + end_position``.
    """

    def __init__(self, elem_size: int) -> None:
        """Initialise an empty array with no recorded end offset.

        Args:
            elem_size (int): Bytes per element, or 0 when it varies.
        """
        super().__init__(elem_size)
        self.end_offset: int = 0
        self.end_position: int = 0

    def parse(self, reader: BinaryIO) -> None:
        """Read the stored end offset, then the array.

        Args:
            reader (BinaryIO): Stream positioned at the end offset.
        """
        self.end_offset = read_int(reader)
        super().parse(reader)
        self.end_position = reader.tell()

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Write the array, back-patching its absolute end offset.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object in
                the output stream.
        """
        offset_pos = writer.tell()
        write_int(writer, 0)  # placeholder end offset
        super().serialize(writer, stream_position)
        self.end_position = writer.tell()
        self.end_offset = stream_position + self.end_position
        resume = writer.tell()
        writer.seek(offset_pos)
        write_int(writer, self.end_offset)
        writer.seek(resume)


class _UnStaticMeshTriangleArray(_UnLazyArray):
    """A static mesh's source triangle list.

    Each triangle's size depends on how many UV sets it carries::

        <3 x vector> <UV set count : int32>
        per UV set: <3 x UV pair : 2 x float32>
        <3 x colour : 4 bytes> <material index : int32> <smoothing mask : uint32>
    """

    def __init__(self) -> None:
        """Initialise an empty, variable-stride triangle list."""
        super().__init__(0)

    def _read_elements(self, reader: BinaryIO, count: int) -> bytes:
        """Read *count* triangles, sizing each from its UV set count.

        Args:
            reader (BinaryIO): Stream positioned at the first triangle.
            count (int): Number of triangles to read.

        Returns:
            bytes: The verbatim triangle bytes.
        """
        out = bytearray()
        for _ in range(count):
            head = _read_exact(reader, 3 * 12 + 4)  # vertices + UV set count
            num_uvs = unpack_int(head[-4:])
            if num_uvs < 0:
                raise ValueError(f"negative UV set count {num_uvs}")
            out += head
            # per-UV-set coordinates, then colours, material index, mask
            out += _read_exact(reader, num_uvs * 3 * 8 + 3 * 4 + 4 + 4)
        return bytes(out)


class _UnStream:
    """A counted native element array followed by its revision counter.

    Covers the vertex, colour and index streams, which differ only in their
    element size::

        <element count : compact index> <elements> <revision : int32>
    """

    def __init__(self, elem_size: int) -> None:
        """Initialise an empty stream.

        Args:
            elem_size (int): Bytes per element.
        """
        self.elements = _UnRawArray(elem_size)
        self.revision: int = 0

    def parse(self, reader: BinaryIO) -> None:
        """Read the elements and the revision counter.

        Args:
            reader (BinaryIO): Stream positioned at the element count.
        """
        self.elements.parse(reader)
        self.revision = read_int(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Write the elements and the revision counter.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        self.elements.serialize(writer, stream_position)
        write_int(writer, self.revision)

    def to_dict(self) -> Dict[str, Any]:
        """Return the stream's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {"elements": self.elements.to_dict(), "revision": self.revision}

    def from_dict(self, data: Any) -> None:
        """Populate the stream from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.elements.from_dict(src.get("elements"))
        self.revision = _as_int(src.get("revision"))


def _walk_bulk_out(node: Any, blob: bytearray) -> None:
    """Move every raw-array payload under *node* into *blob*, in place.

    Each payload's hex text is replaced by its byte length, so the (large)
    element bytes can live in a single sidecar file while the XML keeps only
    the structure.  Traversal follows dict insertion / list order, which the
    XML transport preserves, so :func:`_walk_bulk_in` can restore them.

    Args:
        node (Any): The dict/list tree to rewrite in place.
        blob (bytearray): The accumulating sidecar payload.
    """
    if isinstance(node, dict):
        if _BULK_KEY in node:
            raw = hex_to_bytes(str(node.pop(_BULK_KEY)))
            node[_BULK_LENGTH_KEY] = len(raw)
            blob += raw
            return
        for value in node.values():
            _walk_bulk_out(value, blob)
    elif isinstance(node, list):
        for value in node:
            _walk_bulk_out(value, blob)


def _walk_bulk_in(node: Any, blob: bytes, cursor: List[int]) -> None:
    """Restore raw-array payloads under *node* from *blob*, in place.

    Inverse of :func:`_walk_bulk_out`.

    Args:
        node (Any): The dict/list tree to rewrite in place.
        blob (bytes): The sidecar payload.
        cursor (List[int]): Single-element read position, advanced in place.
    """
    if isinstance(node, dict):
        if _BULK_LENGTH_KEY in node:
            length = _as_int(node.pop(_BULK_LENGTH_KEY))
            start = cursor[0]
            node[_BULK_KEY] = bytes_to_hex(blob[start : start + length])
            cursor[0] = start + length
            return
        for value in node.values():
            _walk_bulk_in(value, blob, cursor)
    elif isinstance(node, list):
        for value in node:
            _walk_bulk_in(value, blob, cursor)


class _UnBulkSidecar:
    """Moves an object's raw-array element bytes into a sidecar ``.bin`` file.

    Mesh payloads are mostly bulk vertex/index/key data.  Keeping it as hex in
    the XML would dwarf the structure it belongs to, so on export every raw
    array's payload is appended to one ``<Class>/<Object>.bin`` file (in
    traversal order) and the XML keeps just its length.
    """

    export: "UnExport"

    def export_xml(self, obj_dict: Dict[str, Any], output_dir: str) -> None:
        """Move bulk element bytes into a sidecar ``.bin`` file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            output_dir (str): The directory to write the sidecar file into.
        """
        blob = bytearray()
        _walk_bulk_out(obj_dict, blob)
        subdir_name = self.__class__.__name__
        bin_filename = self.export.object_name_string + ".bin"
        bin_subdir = os.path.join(output_dir, subdir_name)
        os.makedirs(bin_subdir, exist_ok=True)
        with open(os.path.join(bin_subdir, bin_filename), "wb") as f:
            f.write(bytes(blob))
        obj_dict["bulk_data_file"] = bin_filename

    def import_xml(self, obj_dict: Dict[str, Any], input_dir: str) -> None:
        """Read bulk element bytes back from the sidecar ``.bin`` file.

        Args:
            obj_dict (Dict[str, Any]): The object dictionary to update in place.
            input_dir (str): The directory to read the sidecar file from.
        """
        bin_filename = obj_dict.pop("bulk_data_file", "")
        if not bin_filename:
            return
        subdir_name = self.__class__.__name__
        with open(os.path.join(input_dir, subdir_name, bin_filename), "rb") as f:
            blob = f.read()
        _walk_bulk_in(obj_dict, blob, [0])


class _UnMeshResource(_UnBulkSidecar, _UnContentObject):
    """Base for the mesh resources.

    Adds two things to the standard content object: the sidecar handling for
    bulk element data, and reference walking.  Subclasses declare where their
    object and name references live by overriding ``_object_refs()`` /
    ``_name_refs()``; the resolve, link and name-deduplication passes follow
    those from here instead of each type repeating them.
    """

    def _object_refs(self):
        """Yield every object reference in this resource.

        Yields:
            _UnObjRef: Each object reference, in layout order.
        """
        return iter(())

    def _name_refs(self):
        """Yield every name reference in this resource.

        Yields:
            _UnNameRef: Each name reference, in layout order.
        """
        return iter(())

    def resolve(self) -> None:
        """Resolve every object reference into an item pointer."""
        super().resolve()
        package = self.export.package
        for ref in self._object_refs():
            ref.resolve(package)

    def link(self) -> None:
        """Re-derive every object reference index from its item pointer."""
        super().link()
        package = self.export.package
        for ref in self._object_refs():
            ref.link(package)

    def clear_resolved(self) -> None:
        """Drop every resolved item pointer."""
        super().clear_resolved()
        for ref in self._object_refs():
            ref.item = None

    def clear_links(self) -> None:
        """Zero every object reference index, keeping item pointers."""
        super().clear_links()
        for ref in self._object_refs():
            ref.index = 0

    def deduplicate_names(self) -> None:
        """Re-resolve every name reference after a name-table rebuild."""
        super().deduplicate_names()
        package = self.export.package
        for ref in self._name_refs():
            ref.deduplicate(package)


# ===================================================================== #
#  Mesh resources — object types
# ===================================================================== #


class UnPrimitive(_UnMeshResource):
    """A renderable, collidable geometric resource.

    After the standard object payload come the resource's bounds::

        <bounding box : 2 x vector + validity byte>
        <bounding sphere : centre vector + radius float32>
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise the primitive with empty bounds.

        Args:
            export ("UnExport"): The export entry that owns this primitive.
        """
        super().__init__(export)
        self.bounding_box: Dict[str, Any] = _empty_box()
        self.bounding_sphere: Dict[str, float] = dict.fromkeys(_PLANE_KEYS, 0.0)

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the resource bounds.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.
        """
        super()._parse_tail(reader)
        self.bounding_box = _read_box(reader)
        self.bounding_sphere = _read_float_bundle(reader, _PLANE_KEYS)

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the resource bounds.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        super()._serialize_tail(writer, stream_position)
        _write_box(writer, self.bounding_box)
        _write_float_bundle(writer, self.bounding_sphere, _PLANE_KEYS)

    def to_dict(self) -> Dict[str, Any]:
        """Return the primitive's dict representation.

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        d["bounding_box"] = _box_to_dict(self.bounding_box)
        d["bounding_sphere"] = _float_bundle_to_dict(self.bounding_sphere)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the primitive from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        self.bounding_box = _box_from_dict(data.get("bounding_box"))
        self.bounding_sphere = _float_bundle_from_dict(
            data.get("bounding_sphere"), _PLANE_KEYS
        )


class UnMesh(UnPrimitive):
    """Base for the animated mesh resources.

    Contributes no data of its own to a stored package: its only member is the
    shared default instance, which lives purely at runtime.
    """


class _UnImpostorProps:
    """A mesh's impostor-sprite settings.

    Layout::

        <material : object ref> <relative location : vector>
        <relative rotation : rotator> <scale 3D : vector> <colour : 4 bytes>
        <space mode : int32> <draw mode : int32> <light mode : int32>
    """

    def __init__(self) -> None:
        """Initialise zeroed impostor settings."""
        self.material = _UnObjRef()
        self.relative_location: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.relative_rotation: Dict[str, int] = dict.fromkeys(_ROTATOR_KEYS, 0)
        self.scale_3d: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.color: Dict[str, int] = dict.fromkeys(_COLOR_KEYS, 0)
        self.space_mode: int = 0
        self.draw_mode: int = 0
        self.light_mode: int = 0

    def _object_refs(self):
        """Yield the impostor material reference.

        Yields:
            _UnObjRef: The material reference.
        """
        yield self.material

    def parse(self, reader: BinaryIO) -> None:
        """Parse the impostor settings.

        Args:
            reader (BinaryIO): Stream positioned at the material reference.
        """
        self.material.parse(reader)
        self.relative_location = _read_float_bundle(reader, _VECTOR_KEYS)
        self.relative_rotation = _read_int_bundle(reader, _ROTATOR_KEYS)
        self.scale_3d = _read_float_bundle(reader, _VECTOR_KEYS)
        self.color = _read_byte_bundle(reader, _COLOR_KEYS)
        self.space_mode = read_int(reader)
        self.draw_mode = read_int(reader)
        self.light_mode = read_int(reader)

    def serialize(self, writer: BinaryIO) -> None:
        """Serialise the impostor settings.

        Args:
            writer (BinaryIO): Stream to write to.
        """
        self.material.serialize(writer)
        _write_float_bundle(writer, self.relative_location, _VECTOR_KEYS)
        _write_int_bundle(writer, self.relative_rotation, _ROTATOR_KEYS)
        _write_float_bundle(writer, self.scale_3d, _VECTOR_KEYS)
        _write_byte_bundle(writer, self.color, _COLOR_KEYS)
        write_int(writer, self.space_mode)
        write_int(writer, self.draw_mode)
        write_int(writer, self.light_mode)

    def to_dict(self, owner: "UnObject") -> Dict[str, Any]:
        """Return the impostor settings' dict representation.

        Args:
            owner ("UnObject"): The object that owns these settings.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "material": self.material.to_str(owner),
            "relative_location": _float_bundle_to_dict(self.relative_location),
            "relative_rotation": dict(self.relative_rotation),
            "scale_3d": _float_bundle_to_dict(self.scale_3d),
            "color": dict(self.color),
            "space_mode": self.space_mode,
            "draw_mode": self.draw_mode,
            "light_mode": self.light_mode,
        }

    def from_dict(self, data: Any, owner: "UnObject") -> None:
        """Populate the impostor settings from their dict representation.

        Args:
            data (Any): The transport representation.
            owner ("UnObject"): The object that owns these settings.
        """
        src = data if isinstance(data, dict) else {}
        self.material.from_str(owner, src.get("material"))
        self.relative_location = _float_bundle_from_dict(
            src.get("relative_location"), _VECTOR_KEYS
        )
        self.relative_rotation = _int_bundle_from_dict(
            src.get("relative_rotation"), _ROTATOR_KEYS
        )
        self.scale_3d = _float_bundle_from_dict(src.get("scale_3d"), _VECTOR_KEYS)
        self.color = _int_bundle_from_dict(src.get("color"), _COLOR_KEYS)
        self.space_mode = _as_int(src.get("space_mode"))
        self.draw_mode = _as_int(src.get("draw_mode"))
        self.light_mode = _as_int(src.get("light_mode"))


class UnLodMesh(UnMesh):
    """Base for the level-of-detail mesh resources.

    After the primitive bounds comes the shared LOD geometry::

        <internal version : int32> <model vertex count : int32>
        <packed vertices : 4 bytes each> <materials : object ref array>
        <scale : vector> <origin : vector> <rotation origin : rotator>
        <face LOD levels : 2 bytes each> <faces : 8 bytes each>
        <wedge collapse list : 2 bytes each> <wedges : 10 bytes each>
        <mesh materials : 8 bytes each> <max mesh scale : float32>
        <LOD hysteresis / strength : float32> <LOD min vertices : int32>
        <LOD morph / Z displacement : float32>
        internal version >= 3: <impostor present : int32> <impostor settings>
        internal version >= 4: <skin tesselation factor : float32>

    Internal versions below 2 store two extra legacy arrays that this type does
    not model; such a resource falls back to a verbatim payload.
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise an empty LOD mesh.

        Args:
            export ("UnExport"): The export entry that owns this mesh.
        """
        super().__init__(export)
        self.internal_version: int = 0
        self.model_verts: int = 0
        self.verts = _UnRawArray(4)
        self.materials: List[_UnObjRef] = []
        self.scale: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.origin: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.rot_origin: Dict[str, int] = dict.fromkeys(_ROTATOR_KEYS, 0)
        self.face_level = _UnRawArray(2)
        self.faces = _UnRawArray(8)
        self.collapse_wedge_thus = _UnRawArray(2)
        self.wedges = _UnRawArray(10)
        self.mesh_materials = _UnRawArray(8)
        self.mesh_scale_max: float = 0.0
        self.lod_hysteresis: float = 0.0
        self.lod_strength: float = 0.0
        self.lod_min_verts: int = 0
        self.lod_morph: float = 0.0
        self.lod_z_displace: float = 0.0
        self.impostor_present: int = 0
        self.impostor_props = _UnImpostorProps()
        self.skin_tesselation_factor: float = 0.0

    def _object_refs(self):
        """Yield every object reference in this mesh.

        Yields:
            _UnObjRef: Each object reference, in layout order.
        """
        yield from super()._object_refs()
        yield from self.materials
        yield from self.impostor_props._object_refs()

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the shared LOD geometry.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.

        Raises:
            ValueError: When the internal version predates LOD levels.
        """
        super()._parse_tail(reader)
        self.internal_version = read_int(reader)
        if self.internal_version < 2:
            raise ValueError(
                f"unsupported mesh internal version {self.internal_version}"
            )
        self.model_verts = read_int(reader)
        self.verts.parse(reader)
        self.materials = [_UnObjRef() for _ in range(_read_count(reader))]
        for ref in self.materials:
            ref.parse(reader)
        self.scale = _read_float_bundle(reader, _VECTOR_KEYS)
        self.origin = _read_float_bundle(reader, _VECTOR_KEYS)
        self.rot_origin = _read_int_bundle(reader, _ROTATOR_KEYS)
        self.face_level.parse(reader)
        self.faces.parse(reader)
        self.collapse_wedge_thus.parse(reader)
        self.wedges.parse(reader)
        self.mesh_materials.parse(reader)
        self.mesh_scale_max = read_float(reader)
        self.lod_hysteresis = read_float(reader)
        self.lod_strength = read_float(reader)
        self.lod_min_verts = read_int(reader)
        self.lod_morph = read_float(reader)
        self.lod_z_displace = read_float(reader)
        if self.internal_version >= 3:
            self.impostor_present = read_int(reader)
            self.impostor_props.parse(reader)
        if self.internal_version >= 4:
            self.skin_tesselation_factor = read_float(reader)

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the shared LOD geometry.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        super()._serialize_tail(writer, stream_position)
        write_int(writer, self.internal_version)
        write_int(writer, self.model_verts)
        self.verts.serialize(writer, stream_position)
        write_index(writer, len(self.materials))
        for ref in self.materials:
            ref.serialize(writer)
        _write_float_bundle(writer, self.scale, _VECTOR_KEYS)
        _write_float_bundle(writer, self.origin, _VECTOR_KEYS)
        _write_int_bundle(writer, self.rot_origin, _ROTATOR_KEYS)
        self.face_level.serialize(writer, stream_position)
        self.faces.serialize(writer, stream_position)
        self.collapse_wedge_thus.serialize(writer, stream_position)
        self.wedges.serialize(writer, stream_position)
        self.mesh_materials.serialize(writer, stream_position)
        write_float(writer, self.mesh_scale_max)
        write_float(writer, self.lod_hysteresis)
        write_float(writer, self.lod_strength)
        write_int(writer, self.lod_min_verts)
        write_float(writer, self.lod_morph)
        write_float(writer, self.lod_z_displace)
        if self.internal_version >= 3:
            write_int(writer, self.impostor_present)
            self.impostor_props.serialize(writer)
        if self.internal_version >= 4:
            write_float(writer, self.skin_tesselation_factor)

    def to_dict(self) -> Dict[str, Any]:
        """Return the LOD mesh's dict representation.

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        d["internal_version"] = self.internal_version
        d["model_verts"] = self.model_verts
        d["verts"] = self.verts.to_dict()
        d["materials"] = [ref.to_str(self) for ref in self.materials]
        d["scale"] = _float_bundle_to_dict(self.scale)
        d["origin"] = _float_bundle_to_dict(self.origin)
        d["rot_origin"] = dict(self.rot_origin)
        d["face_level"] = self.face_level.to_dict()
        d["faces"] = self.faces.to_dict()
        d["collapse_wedge_thus"] = self.collapse_wedge_thus.to_dict()
        d["wedges"] = self.wedges.to_dict()
        d["mesh_materials"] = self.mesh_materials.to_dict()
        d["mesh_scale_max"] = format_float32(self.mesh_scale_max)
        d["lod_hysteresis"] = format_float32(self.lod_hysteresis)
        d["lod_strength"] = format_float32(self.lod_strength)
        d["lod_min_verts"] = self.lod_min_verts
        d["lod_morph"] = format_float32(self.lod_morph)
        d["lod_z_displace"] = format_float32(self.lod_z_displace)
        if self.internal_version >= 3:
            d["impostor_present"] = self.impostor_present
            d["impostor_props"] = self.impostor_props.to_dict(self)
        if self.internal_version >= 4:
            d["skin_tesselation_factor"] = format_float32(self.skin_tesselation_factor)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the LOD mesh from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        self.internal_version = _as_int(data.get("internal_version"))
        self.model_verts = _as_int(data.get("model_verts"))
        self.verts.from_dict(data.get("verts"))
        self.materials = []
        for value in _as_list(data.get("materials")):
            ref = _UnObjRef()
            ref.from_str(self, value)
            self.materials.append(ref)
        self.scale = _float_bundle_from_dict(data.get("scale"), _VECTOR_KEYS)
        self.origin = _float_bundle_from_dict(data.get("origin"), _VECTOR_KEYS)
        self.rot_origin = _int_bundle_from_dict(data.get("rot_origin"), _ROTATOR_KEYS)
        self.face_level.from_dict(data.get("face_level"))
        self.faces.from_dict(data.get("faces"))
        self.collapse_wedge_thus.from_dict(data.get("collapse_wedge_thus"))
        self.wedges.from_dict(data.get("wedges"))
        self.mesh_materials.from_dict(data.get("mesh_materials"))
        self.mesh_scale_max = _as_float(data.get("mesh_scale_max"))
        self.lod_hysteresis = _as_float(data.get("lod_hysteresis"))
        self.lod_strength = _as_float(data.get("lod_strength"))
        self.lod_min_verts = _as_int(data.get("lod_min_verts"))
        self.lod_morph = _as_float(data.get("lod_morph"))
        self.lod_z_displace = _as_float(data.get("lod_z_displace"))
        self.impostor_present = _as_int(data.get("impostor_present"))
        self.impostor_props.from_dict(data.get("impostor_props"), self)
        self.skin_tesselation_factor = _as_float(data.get("skin_tesselation_factor"))


class _UnMeshBone:
    """One bone of a skeletal mesh's reference skeleton.

    Layout::

        <name : name ref> <flags : uint32>
        <orientation : quaternion> <position : vector> <sizes : 4 x float32>
        <child count : int32> <parent index : int32>
    """

    def __init__(self) -> None:
        """Initialise a zeroed, unnamed bone."""
        self.name = _UnNameRef()
        self.flags: int = 0
        self.orientation: Dict[str, float] = dict.fromkeys(_PLANE_KEYS, 0.0)
        self.position: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.sizes: List[float] = [0.0] * 4
        self.num_children: int = 0
        self.parent_index: int = 0

    def _name_refs(self):
        """Yield the bone's name reference.

        Yields:
            _UnNameRef: The bone name.
        """
        yield self.name

    def parse(self, reader: BinaryIO, package: "UnPackage") -> None:
        """Parse the bone.

        Args:
            reader (BinaryIO): Stream positioned at the bone name.
            package ("UnPackage"): The owning package.
        """
        self.name.parse(reader, package)
        self.flags = read_uint(reader)
        self.orientation = _read_float_bundle(reader, _PLANE_KEYS)
        self.position = _read_float_bundle(reader, _VECTOR_KEYS)
        self.sizes = [read_float(reader) for _ in range(4)]
        self.num_children = read_int(reader)
        self.parent_index = read_int(reader)

    def serialize(self, writer: BinaryIO, package: "UnPackage") -> None:
        """Serialise the bone.

        Args:
            writer (BinaryIO): Stream to write to.
            package ("UnPackage"): The owning package.
        """
        self.name.serialize(writer, package)
        write_uint(writer, self.flags)
        _write_float_bundle(writer, self.orientation, _PLANE_KEYS)
        _write_float_bundle(writer, self.position, _VECTOR_KEYS)
        for index in range(4):
            write_float(writer, self.sizes[index] if index < len(self.sizes) else 0.0)
        write_int(writer, self.num_children)
        write_int(writer, self.parent_index)

    def to_dict(self, owner: "UnObject") -> Dict[str, Any]:
        """Return the bone's dict representation.

        Args:
            owner ("UnObject"): The object that owns this bone.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "name": self.name.to_str(owner),
            "flags": self.flags,
            "orientation": _float_bundle_to_dict(self.orientation),
            "position": _float_bundle_to_dict(self.position),
            "sizes": [format_float32(value) for value in self.sizes],
            "num_children": self.num_children,
            "parent_index": self.parent_index,
        }

    def from_dict(self, data: Any, owner: "UnObject") -> None:
        """Populate the bone from its dict representation.

        Args:
            data (Any): The transport representation.
            owner ("UnObject"): The object that owns this bone.
        """
        src = data if isinstance(data, dict) else {}
        self.name.from_str(owner, src.get("name"))
        self.flags = _as_int(src.get("flags"))
        self.orientation = _float_bundle_from_dict(src.get("orientation"), _PLANE_KEYS)
        self.position = _float_bundle_from_dict(src.get("position"), _VECTOR_KEYS)
        sizes = _as_list(src.get("sizes"))
        self.sizes = [_as_float(sizes[i]) if i < len(sizes) else 0.0 for i in range(4)]
        self.num_children = _as_int(src.get("num_children"))
        self.parent_index = _as_int(src.get("parent_index"))


class _UnWeightIndex:
    """One entry of a skeletal mesh's legacy weight index.

    Layout::

        <point indices : 2 bytes each> <weight base : int32>
    """

    def __init__(self) -> None:
        """Initialise an empty weight index entry."""
        self.point_indices = _UnRawArray(2)
        self.weight_base: int = 0

    def parse(self, reader: BinaryIO) -> None:
        """Parse the entry.

        Args:
            reader (BinaryIO): Stream positioned at the point index count.
        """
        self.point_indices.parse(reader)
        self.weight_base = read_int(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the entry.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        self.point_indices.serialize(writer, stream_position)
        write_int(writer, self.weight_base)

    def to_dict(self) -> Dict[str, Any]:
        """Return the entry's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "point_indices": self.point_indices.to_dict(),
            "weight_base": self.weight_base,
        }

    def from_dict(self, data: Any) -> None:
        """Populate the entry from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.point_indices.from_dict(src.get("point_indices"))
        self.weight_base = _as_int(src.get("weight_base"))


class _UnSkinVertexStream:
    """A skeletal mesh's rigid-part vertex stream.

    Layout::

        <revision : int32> <partial : int32> <stream callback : int32>
        <vertices : 32 bytes each>
    """

    def __init__(self) -> None:
        """Initialise an empty skin vertex stream."""
        self.revision: int = 0
        self.partial: int = 0
        self.stream_callback: int = 0
        self.vertices = _UnRawArray(32)

    def parse(self, reader: BinaryIO) -> None:
        """Parse the stream.

        Args:
            reader (BinaryIO): Stream positioned at the revision counter.
        """
        self.revision = read_int(reader)
        self.partial = read_int(reader)
        self.stream_callback = read_int(reader)
        self.vertices.parse(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the stream.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        write_int(writer, self.revision)
        write_int(writer, self.partial)
        write_int(writer, self.stream_callback)
        self.vertices.serialize(writer, stream_position)

    def to_dict(self) -> Dict[str, Any]:
        """Return the stream's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "revision": self.revision,
            "partial": self.partial,
            "stream_callback": self.stream_callback,
            "vertices": self.vertices.to_dict(),
        }

    def from_dict(self, data: Any) -> None:
        """Populate the stream from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.revision = _as_int(src.get("revision"))
        self.partial = _as_int(src.get("partial"))
        self.stream_callback = _as_int(src.get("stream_callback"))
        self.vertices.from_dict(src.get("vertices"))


class _UnStaticLodModel:
    """One discrete LOD level of a skeletal mesh.

    Layout::

        <skinning stream : 4 bytes each> <smooth vertices : 16 bytes each>
        <smooth stream wedge count : int32>
        <smooth sections : 18 bytes each> <rigid sections : 18 bytes each>
        <smooth index buffer> <rigid index buffer> <rigid vertex stream>
        <influences / wedges / faces / points : lazy arrays>
        <display factor : float32> <LOD hysteresis : float32>
        <duplicate vertex count : int32> <max influences : int32>
        <unique subset : int32> <use smoothing : int32>
    """

    def __init__(self) -> None:
        """Initialise an empty LOD model."""
        self.skinning_stream = _UnRawArray(4)
        self.smooth_verts = _UnRawArray(16)
        self.smooth_stream_wedges: int = 0
        self.smooth_sections = _UnRawArray(18)
        self.rigid_sections = _UnRawArray(18)
        self.smooth_index_buffer = _UnStream(2)
        self.rigid_index_buffer = _UnStream(2)
        self.rigid_vertex_stream = _UnSkinVertexStream()
        self.influences = _UnLazyArray(8)
        self.wedges = _UnLazyArray(10)
        self.faces = _UnLazyArray(8)
        self.points = _UnLazyArray(12)
        self.display_factor: float = 0.0
        self.lod_hysteresis: float = 0.0
        self.dup_vert_count: int = 0
        self.max_influences: int = 0
        self.unique_subset: int = 0
        self.use_smoothing: int = 0

    def parse(self, reader: BinaryIO) -> None:
        """Parse the LOD model.

        Args:
            reader (BinaryIO): Stream positioned at the skinning stream.
        """
        self.skinning_stream.parse(reader)
        self.smooth_verts.parse(reader)
        self.smooth_stream_wedges = read_int(reader)
        self.smooth_sections.parse(reader)
        self.rigid_sections.parse(reader)
        self.smooth_index_buffer.parse(reader)
        self.rigid_index_buffer.parse(reader)
        self.rigid_vertex_stream.parse(reader)
        self.influences.parse(reader)
        self.wedges.parse(reader)
        self.faces.parse(reader)
        self.points.parse(reader)
        self.display_factor = read_float(reader)
        self.lod_hysteresis = read_float(reader)
        self.dup_vert_count = read_int(reader)
        self.max_influences = read_int(reader)
        self.unique_subset = read_int(reader)
        self.use_smoothing = read_int(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the LOD model.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        self.skinning_stream.serialize(writer, stream_position)
        self.smooth_verts.serialize(writer, stream_position)
        write_int(writer, self.smooth_stream_wedges)
        self.smooth_sections.serialize(writer, stream_position)
        self.rigid_sections.serialize(writer, stream_position)
        self.smooth_index_buffer.serialize(writer, stream_position)
        self.rigid_index_buffer.serialize(writer, stream_position)
        self.rigid_vertex_stream.serialize(writer, stream_position)
        self.influences.serialize(writer, stream_position)
        self.wedges.serialize(writer, stream_position)
        self.faces.serialize(writer, stream_position)
        self.points.serialize(writer, stream_position)
        write_float(writer, self.display_factor)
        write_float(writer, self.lod_hysteresis)
        write_int(writer, self.dup_vert_count)
        write_int(writer, self.max_influences)
        write_int(writer, self.unique_subset)
        write_int(writer, self.use_smoothing)

    def to_dict(self) -> Dict[str, Any]:
        """Return the LOD model's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "skinning_stream": self.skinning_stream.to_dict(),
            "smooth_verts": self.smooth_verts.to_dict(),
            "smooth_stream_wedges": self.smooth_stream_wedges,
            "smooth_sections": self.smooth_sections.to_dict(),
            "rigid_sections": self.rigid_sections.to_dict(),
            "smooth_index_buffer": self.smooth_index_buffer.to_dict(),
            "rigid_index_buffer": self.rigid_index_buffer.to_dict(),
            "rigid_vertex_stream": self.rigid_vertex_stream.to_dict(),
            "influences": self.influences.to_dict(),
            "wedges": self.wedges.to_dict(),
            "faces": self.faces.to_dict(),
            "points": self.points.to_dict(),
            "display_factor": format_float32(self.display_factor),
            "lod_hysteresis": format_float32(self.lod_hysteresis),
            "dup_vert_count": self.dup_vert_count,
            "max_influences": self.max_influences,
            "unique_subset": self.unique_subset,
            "use_smoothing": self.use_smoothing,
        }

    def from_dict(self, data: Any) -> None:
        """Populate the LOD model from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.skinning_stream.from_dict(src.get("skinning_stream"))
        self.smooth_verts.from_dict(src.get("smooth_verts"))
        self.smooth_stream_wedges = _as_int(src.get("smooth_stream_wedges"))
        self.smooth_sections.from_dict(src.get("smooth_sections"))
        self.rigid_sections.from_dict(src.get("rigid_sections"))
        self.smooth_index_buffer.from_dict(src.get("smooth_index_buffer"))
        self.rigid_index_buffer.from_dict(src.get("rigid_index_buffer"))
        self.rigid_vertex_stream.from_dict(src.get("rigid_vertex_stream"))
        self.influences.from_dict(src.get("influences"))
        self.wedges.from_dict(src.get("wedges"))
        self.faces.from_dict(src.get("faces"))
        self.points.from_dict(src.get("points"))
        self.display_factor = _as_float(src.get("display_factor"))
        self.lod_hysteresis = _as_float(src.get("lod_hysteresis"))
        self.dup_vert_count = _as_int(src.get("dup_vert_count"))
        self.max_influences = _as_int(src.get("max_influences"))
        self.unique_subset = _as_int(src.get("unique_subset"))
        self.use_smoothing = _as_int(src.get("use_smoothing"))


class _UnSkelBonePrimitive:
    """Base for a skeletal mesh's per-bone collision primitives.

    Layout::

        <bone name : name ref> <offset : vector> <extent>
        package version >= 124: <blocks karma : int32>
        package version >= 125: <blocks non-zero extent : int32>
                                <blocks zero extent : int32>

    Subclasses supply the primitive-specific *extent* field.
    """

    def __init__(self) -> None:
        """Initialise an unnamed primitive at the bone origin."""
        self.bone_name = _UnNameRef()
        self.offset: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.block_karma: int = 0
        self.block_non_zero_extent: int = 0
        self.block_zero_extent: int = 0

    def _name_refs(self):
        """Yield the primitive's bone name reference.

        Yields:
            _UnNameRef: The bone name.
        """
        yield self.bone_name

    def _parse_extent(self, reader: BinaryIO) -> None:
        """Parse the primitive-specific extent.

        Args:
            reader (BinaryIO): Stream positioned at the extent.
        """
        raise NotImplementedError

    def _serialize_extent(self, writer: BinaryIO) -> None:
        """Serialise the primitive-specific extent.

        Args:
            writer (BinaryIO): Stream to write to.
        """
        raise NotImplementedError

    def _extent_to_dict(self) -> Dict[str, Any]:
        """Return the extent's dict representation.

        Returns:
            Dict[str, Any]: The extent fields.
        """
        raise NotImplementedError

    def _extent_from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the extent from its dict representation.

        Args:
            data (Dict[str, Any]): The extent fields.
        """
        raise NotImplementedError

    def parse(self, reader: BinaryIO, package: "UnPackage") -> None:
        """Parse the collision primitive.

        Args:
            reader (BinaryIO): Stream positioned at the bone name.
            package ("UnPackage"): The owning package.
        """
        self.bone_name.parse(reader, package)
        self.offset = _read_float_bundle(reader, _VECTOR_KEYS)
        self._parse_extent(reader)
        if package.version >= 124:
            self.block_karma = read_int(reader)
        if package.version >= 125:
            self.block_non_zero_extent = read_int(reader)
            self.block_zero_extent = read_int(reader)

    def serialize(self, writer: BinaryIO, package: "UnPackage") -> None:
        """Serialise the collision primitive.

        Args:
            writer (BinaryIO): Stream to write to.
            package ("UnPackage"): The owning package.
        """
        self.bone_name.serialize(writer, package)
        _write_float_bundle(writer, self.offset, _VECTOR_KEYS)
        self._serialize_extent(writer)
        if package.version >= 124:
            write_int(writer, self.block_karma)
        if package.version >= 125:
            write_int(writer, self.block_non_zero_extent)
            write_int(writer, self.block_zero_extent)

    def to_dict(self, owner: "UnObject") -> Dict[str, Any]:
        """Return the primitive's dict representation.

        Args:
            owner ("UnObject"): The object that owns this primitive.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        d: Dict[str, Any] = {
            "bone_name": self.bone_name.to_str(owner),
            "offset": _float_bundle_to_dict(self.offset),
        }
        d.update(self._extent_to_dict())
        d["block_karma"] = self.block_karma
        d["block_non_zero_extent"] = self.block_non_zero_extent
        d["block_zero_extent"] = self.block_zero_extent
        return d

    def from_dict(self, data: Any, owner: "UnObject") -> None:
        """Populate the primitive from its dict representation.

        Args:
            data (Any): The transport representation.
            owner ("UnObject"): The object that owns this primitive.
        """
        src = data if isinstance(data, dict) else {}
        self.bone_name.from_str(owner, src.get("bone_name"))
        self.offset = _float_bundle_from_dict(src.get("offset"), _VECTOR_KEYS)
        self._extent_from_dict(src)
        self.block_karma = _as_int(src.get("block_karma"))
        self.block_non_zero_extent = _as_int(src.get("block_non_zero_extent"))
        self.block_zero_extent = _as_int(src.get("block_zero_extent"))


class _UnSkelBoneSphere(_UnSkelBonePrimitive):
    """A per-bone collision sphere, whose extent is a single radius."""

    def __init__(self) -> None:
        """Initialise a zero-radius sphere."""
        super().__init__()
        self.radius: float = 0.0

    def _parse_extent(self, reader: BinaryIO) -> None:
        """Parse the sphere radius.

        Args:
            reader (BinaryIO): Stream positioned at the radius.
        """
        self.radius = read_float(reader)

    def _serialize_extent(self, writer: BinaryIO) -> None:
        """Serialise the sphere radius.

        Args:
            writer (BinaryIO): Stream to write to.
        """
        write_float(writer, self.radius)

    def _extent_to_dict(self) -> Dict[str, Any]:
        """Return the radius as a dict entry.

        Returns:
            Dict[str, Any]: The radius field.
        """
        return {"radius": format_float32(self.radius)}

    def _extent_from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the radius from its dict entry.

        Args:
            data (Dict[str, Any]): The extent fields.
        """
        self.radius = _as_float(data.get("radius"))


class _UnSkelBoneBox(_UnSkelBonePrimitive):
    """A per-bone collision box, whose extent is a half-size vector."""

    def __init__(self) -> None:
        """Initialise a zero-sized box."""
        super().__init__()
        self.radii: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)

    def _parse_extent(self, reader: BinaryIO) -> None:
        """Parse the box half-sizes.

        Args:
            reader (BinaryIO): Stream positioned at the half-size vector.
        """
        self.radii = _read_float_bundle(reader, _VECTOR_KEYS)

    def _serialize_extent(self, writer: BinaryIO) -> None:
        """Serialise the box half-sizes.

        Args:
            writer (BinaryIO): Stream to write to.
        """
        _write_float_bundle(writer, self.radii, _VECTOR_KEYS)

    def _extent_to_dict(self) -> Dict[str, Any]:
        """Return the half-sizes as a dict entry.

        Returns:
            Dict[str, Any]: The half-size field.
        """
        return {"radii": _float_bundle_to_dict(self.radii)}

    def _extent_from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the half-sizes from their dict entry.

        Args:
            data (Dict[str, Any]): The extent fields.
        """
        self.radii = _float_bundle_from_dict(data.get("radii"), _VECTOR_KEYS)


class UnSkeletalMesh(UnLodMesh):
    """A bone-animated mesh resource.

    After the shared LOD geometry come the skeletal data::

        <points : 12 bytes each> <reference skeleton : bone list>
        <default animation : object ref> <skeletal depth : int32>
        <legacy weight index : entry list> <weights : 4 bytes each>
        <attachment aliases : name array> <attachment bones : name array>
        <attachment coordinates : 48 bytes each>
        <LOD models> <default reference mesh : object ref>
        <raw vertices / wedges / faces / influences
         / wedge collapse list / face LOD levels : lazy arrays>
        package version >= 120: <authentication key : uint32>
        package version >= 122: <karma properties : object ref>
                                <collision spheres> <collision boxes>
                                <collision box models : object ref array>
        package version >= 127: <collision proxy mesh : object ref>
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise an empty skeletal mesh.

        Args:
            export ("UnExport"): The export entry that owns this mesh.
        """
        super().__init__(export)
        self.points = _UnRawArray(12)
        self.ref_skeleton: List[_UnMeshBone] = []
        self.default_anim = _UnObjRef()
        self.skeletal_depth: int = 0
        self.multi_blends: List[_UnWeightIndex] = []
        self.weights = _UnRawArray(4)
        self.tag_aliases: List[_UnNameRef] = []
        self.tag_names: List[_UnNameRef] = []
        self.tag_coords = _UnRawArray(48)
        self.lod_models: List[_UnStaticLodModel] = []
        self.default_ref_mesh = _UnObjRef()
        self.raw_verts = _UnLazyArray(12)
        self.raw_wedges = _UnLazyArray(10)
        self.raw_faces = _UnLazyArray(12)
        self.raw_influences = _UnLazyArray(8)
        self.raw_collapse_wedges = _UnLazyArray(2)
        self.raw_face_level = _UnLazyArray(2)
        self.authentication_key: int = 0
        self.k_physics_props = _UnObjRef()
        self.bone_collision_spheres: List[_UnSkelBoneSphere] = []
        self.bone_collision_boxes: List[_UnSkelBoneBox] = []
        self.bone_collision_box_models: List[_UnObjRef] = []
        self.collision_static_mesh = _UnObjRef()

    def _object_refs(self):
        """Yield every object reference in this mesh.

        Yields:
            _UnObjRef: Each object reference, in layout order.
        """
        yield from super()._object_refs()
        yield self.default_anim
        yield self.default_ref_mesh
        yield self.k_physics_props
        yield from self.bone_collision_box_models
        yield self.collision_static_mesh

    def _name_refs(self):
        """Yield every name reference in this mesh.

        Yields:
            _UnNameRef: Each name reference, in layout order.
        """
        yield from super()._name_refs()
        for bone in self.ref_skeleton:
            yield from bone._name_refs()
        yield from self.tag_aliases
        yield from self.tag_names
        for sphere in self.bone_collision_spheres:
            yield from sphere._name_refs()
        for box in self.bone_collision_boxes:
            yield from box._name_refs()

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the skeletal mesh data.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.
        """
        super()._parse_tail(reader)
        package = self.export.package
        self.points.parse(reader)
        self.ref_skeleton = [_UnMeshBone() for _ in range(_read_count(reader))]
        for bone in self.ref_skeleton:
            bone.parse(reader, package)
        self.default_anim.parse(reader)
        self.skeletal_depth = read_int(reader)
        self.multi_blends = [_UnWeightIndex() for _ in range(_read_count(reader))]
        for entry in self.multi_blends:
            entry.parse(reader)
        self.weights.parse(reader)
        self.tag_aliases = _parse_name_array(reader, package)
        self.tag_names = _parse_name_array(reader, package)
        self.tag_coords.parse(reader)
        self.lod_models = [_UnStaticLodModel() for _ in range(_read_count(reader))]
        for model in self.lod_models:
            model.parse(reader)
        self.default_ref_mesh.parse(reader)
        self.raw_verts.parse(reader)
        self.raw_wedges.parse(reader)
        self.raw_faces.parse(reader)
        self.raw_influences.parse(reader)
        self.raw_collapse_wedges.parse(reader)
        self.raw_face_level.parse(reader)
        if package.version >= 120:
            self.authentication_key = read_uint(reader)
        if package.version >= 122:
            self.k_physics_props.parse(reader)
            self.bone_collision_spheres = [
                _UnSkelBoneSphere() for _ in range(_read_count(reader))
            ]
            for sphere in self.bone_collision_spheres:
                sphere.parse(reader, package)
            self.bone_collision_boxes = [
                _UnSkelBoneBox() for _ in range(_read_count(reader))
            ]
            for box in self.bone_collision_boxes:
                box.parse(reader, package)
            self.bone_collision_box_models = [
                _UnObjRef() for _ in range(_read_count(reader))
            ]
            for ref in self.bone_collision_box_models:
                ref.parse(reader)
        if package.version >= 127:
            self.collision_static_mesh.parse(reader)

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the skeletal mesh data.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        super()._serialize_tail(writer, stream_position)
        package = self.export.package
        self.points.serialize(writer, stream_position)
        write_index(writer, len(self.ref_skeleton))
        for bone in self.ref_skeleton:
            bone.serialize(writer, package)
        self.default_anim.serialize(writer)
        write_int(writer, self.skeletal_depth)
        write_index(writer, len(self.multi_blends))
        for entry in self.multi_blends:
            entry.serialize(writer, stream_position)
        self.weights.serialize(writer, stream_position)
        _serialize_name_array(writer, package, self.tag_aliases)
        _serialize_name_array(writer, package, self.tag_names)
        self.tag_coords.serialize(writer, stream_position)
        write_index(writer, len(self.lod_models))
        for model in self.lod_models:
            model.serialize(writer, stream_position)
        self.default_ref_mesh.serialize(writer)
        self.raw_verts.serialize(writer, stream_position)
        self.raw_wedges.serialize(writer, stream_position)
        self.raw_faces.serialize(writer, stream_position)
        self.raw_influences.serialize(writer, stream_position)
        self.raw_collapse_wedges.serialize(writer, stream_position)
        self.raw_face_level.serialize(writer, stream_position)
        if package.version >= 120:
            write_uint(writer, self.authentication_key)
        if package.version >= 122:
            self.k_physics_props.serialize(writer)
            write_index(writer, len(self.bone_collision_spheres))
            for sphere in self.bone_collision_spheres:
                sphere.serialize(writer, package)
            write_index(writer, len(self.bone_collision_boxes))
            for box in self.bone_collision_boxes:
                box.serialize(writer, package)
            write_index(writer, len(self.bone_collision_box_models))
            for ref in self.bone_collision_box_models:
                ref.serialize(writer)
        if package.version >= 127:
            self.collision_static_mesh.serialize(writer)

    def to_dict(self) -> Dict[str, Any]:
        """Return the skeletal mesh's dict representation.

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        d["points"] = self.points.to_dict()
        d["ref_skeleton"] = [bone.to_dict(self) for bone in self.ref_skeleton]
        d["default_anim"] = self.default_anim.to_str(self)
        d["skeletal_depth"] = self.skeletal_depth
        d["multi_blends"] = [entry.to_dict() for entry in self.multi_blends]
        d["weights"] = self.weights.to_dict()
        d["tag_aliases"] = [ref.to_str(self) for ref in self.tag_aliases]
        d["tag_names"] = [ref.to_str(self) for ref in self.tag_names]
        d["tag_coords"] = self.tag_coords.to_dict()
        d["lod_models"] = [model.to_dict() for model in self.lod_models]
        d["default_ref_mesh"] = self.default_ref_mesh.to_str(self)
        d["raw_verts"] = self.raw_verts.to_dict()
        d["raw_wedges"] = self.raw_wedges.to_dict()
        d["raw_faces"] = self.raw_faces.to_dict()
        d["raw_influences"] = self.raw_influences.to_dict()
        d["raw_collapse_wedges"] = self.raw_collapse_wedges.to_dict()
        d["raw_face_level"] = self.raw_face_level.to_dict()
        d["authentication_key"] = self.authentication_key
        d["k_physics_props"] = self.k_physics_props.to_str(self)
        d["bone_collision_spheres"] = [
            sphere.to_dict(self) for sphere in self.bone_collision_spheres
        ]
        d["bone_collision_boxes"] = [
            box.to_dict(self) for box in self.bone_collision_boxes
        ]
        d["bone_collision_box_models"] = [
            ref.to_str(self) for ref in self.bone_collision_box_models
        ]
        d["collision_static_mesh"] = self.collision_static_mesh.to_str(self)
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the skeletal mesh from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        self.points.from_dict(data.get("points"))
        self.ref_skeleton = []
        for value in _as_list(data.get("ref_skeleton")):
            bone = _UnMeshBone()
            bone.from_dict(value, self)
            self.ref_skeleton.append(bone)
        self.default_anim.from_str(self, data.get("default_anim"))
        self.skeletal_depth = _as_int(data.get("skeletal_depth"))
        self.multi_blends = []
        for value in _as_list(data.get("multi_blends")):
            entry = _UnWeightIndex()
            entry.from_dict(value)
            self.multi_blends.append(entry)
        self.weights.from_dict(data.get("weights"))
        self.tag_aliases = _name_array_from_list(self, data.get("tag_aliases"))
        self.tag_names = _name_array_from_list(self, data.get("tag_names"))
        self.tag_coords.from_dict(data.get("tag_coords"))
        self.lod_models = []
        for value in _as_list(data.get("lod_models")):
            model = _UnStaticLodModel()
            model.from_dict(value)
            self.lod_models.append(model)
        self.default_ref_mesh.from_str(self, data.get("default_ref_mesh"))
        self.raw_verts.from_dict(data.get("raw_verts"))
        self.raw_wedges.from_dict(data.get("raw_wedges"))
        self.raw_faces.from_dict(data.get("raw_faces"))
        self.raw_influences.from_dict(data.get("raw_influences"))
        self.raw_collapse_wedges.from_dict(data.get("raw_collapse_wedges"))
        self.raw_face_level.from_dict(data.get("raw_face_level"))
        self.authentication_key = _as_int(data.get("authentication_key"))
        self.k_physics_props.from_str(self, data.get("k_physics_props"))
        self.bone_collision_spheres = []
        for value in _as_list(data.get("bone_collision_spheres")):
            sphere = _UnSkelBoneSphere()
            sphere.from_dict(value, self)
            self.bone_collision_spheres.append(sphere)
        self.bone_collision_boxes = []
        for value in _as_list(data.get("bone_collision_boxes")):
            box = _UnSkelBoneBox()
            box.from_dict(value, self)
            self.bone_collision_boxes.append(box)
        self.bone_collision_box_models = []
        for value in _as_list(data.get("bone_collision_box_models")):
            ref = _UnObjRef()
            ref.from_str(self, value)
            self.bone_collision_box_models.append(ref)
        self.collision_static_mesh.from_str(self, data.get("collision_static_mesh"))


def _parse_name_array(reader: BinaryIO, package: "UnPackage") -> List[_UnNameRef]:
    """Parse a counted array of name references.

    Args:
        reader (BinaryIO): Stream positioned at the element count.
        package ("UnPackage"): The owning package.

    Returns:
        List[_UnNameRef]: The parsed name references.
    """
    refs = [_UnNameRef() for _ in range(_read_count(reader))]
    for ref in refs:
        ref.parse(reader, package)
    return refs


def _serialize_name_array(
    writer: BinaryIO, package: "UnPackage", refs: List[_UnNameRef]
) -> None:
    """Serialise a counted array of name references.

    Args:
        writer (BinaryIO): Stream to write to.
        package ("UnPackage"): The owning package.
        refs (List[_UnNameRef]): The name references to write.
    """
    write_index(writer, len(refs))
    for ref in refs:
        ref.serialize(writer, package)


def _name_array_from_list(owner: "UnObject", data: Any) -> List[_UnNameRef]:
    """Rebuild an array of name references from its dict representation.

    Args:
        owner ("UnObject"): The object that owns the references.
        data (Any): The list of name strings.

    Returns:
        List[_UnNameRef]: The rebuilt name references.
    """
    refs: List[_UnNameRef] = []
    for value in _as_list(data):
        ref = _UnNameRef()
        ref.from_str(owner, value)
        refs.append(ref)
    return refs


class _UnAnalogTrack:
    """One bone's compressed animation track.

    Layout::

        <flags : uint32> <orientation keys : 16 bytes each>
        <position keys : 12 bytes each> <key times : 4 bytes each>
    """

    def __init__(self) -> None:
        """Initialise an empty track."""
        self.flags: int = 0
        self.key_quat = _UnRawArray(16)
        self.key_pos = _UnRawArray(12)
        self.key_time = _UnRawArray(4)

    def parse(self, reader: BinaryIO) -> None:
        """Parse the track.

        Args:
            reader (BinaryIO): Stream positioned at the flags.
        """
        self.flags = read_uint(reader)
        self.key_quat.parse(reader)
        self.key_pos.parse(reader)
        self.key_time.parse(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the track.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        write_uint(writer, self.flags)
        self.key_quat.serialize(writer, stream_position)
        self.key_pos.serialize(writer, stream_position)
        self.key_time.serialize(writer, stream_position)

    def to_dict(self) -> Dict[str, Any]:
        """Return the track's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "flags": self.flags,
            "key_quat": self.key_quat.to_dict(),
            "key_pos": self.key_pos.to_dict(),
            "key_time": self.key_time.to_dict(),
        }

    def from_dict(self, data: Any) -> None:
        """Populate the track from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.flags = _as_int(src.get("flags"))
        self.key_quat.from_dict(src.get("key_quat"))
        self.key_pos.from_dict(src.get("key_pos"))
        self.key_time.from_dict(src.get("key_time"))


class _UnMotionChunk:
    """One animation sequence's compressed key tracks.

    Layout::

        <root speed : vector> <track time : float32> <start bone : int32>
        <flags : uint32> <bone track index : 4 bytes each>
        <bone tracks> <root track>
    """

    def __init__(self) -> None:
        """Initialise an empty motion chunk."""
        self.root_speed_3d: Dict[str, float] = dict.fromkeys(_VECTOR_KEYS, 0.0)
        self.track_time: float = 0.0
        self.start_bone: int = 0
        self.flags: int = 0
        self.bone_indices = _UnRawArray(4)
        self.anim_tracks: List[_UnAnalogTrack] = []
        self.root_track = _UnAnalogTrack()

    def parse(self, reader: BinaryIO) -> None:
        """Parse the motion chunk.

        Args:
            reader (BinaryIO): Stream positioned at the root speed.
        """
        self.root_speed_3d = _read_float_bundle(reader, _VECTOR_KEYS)
        self.track_time = read_float(reader)
        self.start_bone = read_int(reader)
        self.flags = read_uint(reader)
        self.bone_indices.parse(reader)
        self.anim_tracks = [_UnAnalogTrack() for _ in range(_read_count(reader))]
        for track in self.anim_tracks:
            track.parse(reader)
        self.root_track.parse(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the motion chunk.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        _write_float_bundle(writer, self.root_speed_3d, _VECTOR_KEYS)
        write_float(writer, self.track_time)
        write_int(writer, self.start_bone)
        write_uint(writer, self.flags)
        self.bone_indices.serialize(writer, stream_position)
        write_index(writer, len(self.anim_tracks))
        for track in self.anim_tracks:
            track.serialize(writer, stream_position)
        self.root_track.serialize(writer, stream_position)

    def to_dict(self) -> Dict[str, Any]:
        """Return the motion chunk's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "root_speed_3d": _float_bundle_to_dict(self.root_speed_3d),
            "track_time": format_float32(self.track_time),
            "start_bone": self.start_bone,
            "flags": self.flags,
            "bone_indices": self.bone_indices.to_dict(),
            "anim_tracks": [track.to_dict() for track in self.anim_tracks],
            "root_track": self.root_track.to_dict(),
        }

    def from_dict(self, data: Any) -> None:
        """Populate the motion chunk from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.root_speed_3d = _float_bundle_from_dict(
            src.get("root_speed_3d"), _VECTOR_KEYS
        )
        self.track_time = _as_float(src.get("track_time"))
        self.start_bone = _as_int(src.get("start_bone"))
        self.flags = _as_int(src.get("flags"))
        self.bone_indices.from_dict(src.get("bone_indices"))
        self.anim_tracks = []
        for value in _as_list(src.get("anim_tracks")):
            track = _UnAnalogTrack()
            track.from_dict(value)
            self.anim_tracks.append(track)
        self.root_track.from_dict(src.get("root_track"))


class _UnMeshAnimNotify:
    """One notification fired at a point within an animation sequence.

    Layout::

        <time : float32> <function : name ref>
        package version >= 112: <notify object : object ref>
    """

    def __init__(self) -> None:
        """Initialise an empty notification."""
        self.time: float = 0.0
        self.function = _UnNameRef()
        self.notify_object = _UnObjRef()

    def _object_refs(self):
        """Yield the notification's object reference.

        Yields:
            _UnObjRef: The notify object.
        """
        yield self.notify_object

    def _name_refs(self):
        """Yield the notification's function name reference.

        Yields:
            _UnNameRef: The function name.
        """
        yield self.function

    def parse(self, reader: BinaryIO, package: "UnPackage") -> None:
        """Parse the notification.

        Args:
            reader (BinaryIO): Stream positioned at the time.
            package ("UnPackage"): The owning package.
        """
        self.time = read_float(reader)
        self.function.parse(reader, package)
        if package.version >= 112:
            self.notify_object.parse(reader)

    def serialize(self, writer: BinaryIO, package: "UnPackage") -> None:
        """Serialise the notification.

        Args:
            writer (BinaryIO): Stream to write to.
            package ("UnPackage"): The owning package.
        """
        write_float(writer, self.time)
        self.function.serialize(writer, package)
        if package.version >= 112:
            self.notify_object.serialize(writer)

    def to_dict(self, owner: "UnObject") -> Dict[str, Any]:
        """Return the notification's dict representation.

        Args:
            owner ("UnObject"): The object that owns this notification.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "time": format_float32(self.time),
            "function": self.function.to_str(owner),
            "notify_object": self.notify_object.to_str(owner),
        }

    def from_dict(self, data: Any, owner: "UnObject") -> None:
        """Populate the notification from its dict representation.

        Args:
            data (Any): The transport representation.
            owner ("UnObject"): The object that owns this notification.
        """
        src = data if isinstance(data, dict) else {}
        self.time = _as_float(src.get("time"))
        self.function.from_str(owner, src.get("function"))
        self.notify_object.from_str(owner, src.get("notify_object"))


class _UnMeshAnimSeq:
    """One named animation sequence.

    Layout::

        package version >= 115: <bookmark : float32>
        <name : name ref> <groups : name array>
        <start frame : int32> <frame count : int32>
        <notifications> <rate : float32>
    """

    def __init__(self) -> None:
        """Initialise an empty sequence."""
        self.bookmark: float = 0.0
        self.name = _UnNameRef()
        self.groups: List[_UnNameRef] = []
        self.start_frame: int = 0
        self.num_frames: int = 0
        self.notifys: List[_UnMeshAnimNotify] = []
        self.rate: float = 0.0

    def _object_refs(self):
        """Yield every object reference in this sequence.

        Yields:
            _UnObjRef: Each notification's notify object.
        """
        for notify in self.notifys:
            yield from notify._object_refs()

    def _name_refs(self):
        """Yield every name reference in this sequence.

        Yields:
            _UnNameRef: Each name reference, in layout order.
        """
        yield self.name
        yield from self.groups
        for notify in self.notifys:
            yield from notify._name_refs()

    def parse(self, reader: BinaryIO, package: "UnPackage") -> None:
        """Parse the sequence.

        Args:
            reader (BinaryIO): Stream positioned at the sequence.
            package ("UnPackage"): The owning package.
        """
        if package.version >= 115:
            self.bookmark = read_float(reader)
        self.name.parse(reader, package)
        self.groups = _parse_name_array(reader, package)
        self.start_frame = read_int(reader)
        self.num_frames = read_int(reader)
        self.notifys = [_UnMeshAnimNotify() for _ in range(_read_count(reader))]
        for notify in self.notifys:
            notify.parse(reader, package)
        self.rate = read_float(reader)

    def serialize(self, writer: BinaryIO, package: "UnPackage") -> None:
        """Serialise the sequence.

        Args:
            writer (BinaryIO): Stream to write to.
            package ("UnPackage"): The owning package.
        """
        if package.version >= 115:
            write_float(writer, self.bookmark)
        self.name.serialize(writer, package)
        _serialize_name_array(writer, package, self.groups)
        write_int(writer, self.start_frame)
        write_int(writer, self.num_frames)
        write_index(writer, len(self.notifys))
        for notify in self.notifys:
            notify.serialize(writer, package)
        write_float(writer, self.rate)

    def to_dict(self, owner: "UnObject") -> Dict[str, Any]:
        """Return the sequence's dict representation.

        Args:
            owner ("UnObject"): The object that owns this sequence.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "bookmark": format_float32(self.bookmark),
            "name": self.name.to_str(owner),
            "groups": [ref.to_str(owner) for ref in self.groups],
            "start_frame": self.start_frame,
            "num_frames": self.num_frames,
            "notifys": [notify.to_dict(owner) for notify in self.notifys],
            "rate": format_float32(self.rate),
        }

    def from_dict(self, data: Any, owner: "UnObject") -> None:
        """Populate the sequence from its dict representation.

        Args:
            data (Any): The transport representation.
            owner ("UnObject"): The object that owns this sequence.
        """
        src = data if isinstance(data, dict) else {}
        self.bookmark = _as_float(src.get("bookmark"))
        self.name.from_str(owner, src.get("name"))
        self.groups = _name_array_from_list(owner, src.get("groups"))
        self.start_frame = _as_int(src.get("start_frame"))
        self.num_frames = _as_int(src.get("num_frames"))
        self.notifys = []
        for value in _as_list(src.get("notifys")):
            notify = _UnMeshAnimNotify()
            notify.from_dict(value, owner)
            self.notifys.append(notify)
        self.rate = _as_float(src.get("rate"))


class UnMeshAnimation(_UnMeshResource):
    """A set of skeletal animation sequences.

    Bones are linked to a skeletal mesh's reference skeleton by name at
    runtime, so the resource stands on its own.  After the standard object
    payload comes::

        <internal version : int32> <bones : name / flags / parent triples>
        <motion chunks> <sequences>
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise an empty animation set.

        Args:
            export ("UnExport"): The export entry that owns this animation set.
        """
        super().__init__(export)
        self.internal_version: int = 0
        self.ref_bones: List[_UnNamedBone] = []
        self.moves: List[_UnMotionChunk] = []
        self.anim_seqs: List[_UnMeshAnimSeq] = []

    def _object_refs(self):
        """Yield every object reference in this animation set.

        Yields:
            _UnObjRef: Each object reference, in layout order.
        """
        for seq in self.anim_seqs:
            yield from seq._object_refs()

    def _name_refs(self):
        """Yield every name reference in this animation set.

        Yields:
            _UnNameRef: Each name reference, in layout order.
        """
        for bone in self.ref_bones:
            yield from bone._name_refs()
        for seq in self.anim_seqs:
            yield from seq._name_refs()

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the animation set.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.
        """
        super()._parse_tail(reader)
        package = self.export.package
        self.internal_version = read_int(reader)
        self.ref_bones = [_UnNamedBone() for _ in range(_read_count(reader))]
        for bone in self.ref_bones:
            bone.parse(reader, package)
        self.moves = [_UnMotionChunk() for _ in range(_read_count(reader))]
        for chunk in self.moves:
            chunk.parse(reader)
        self.anim_seqs = [_UnMeshAnimSeq() for _ in range(_read_count(reader))]
        for seq in self.anim_seqs:
            seq.parse(reader, package)

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the animation set.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        super()._serialize_tail(writer, stream_position)
        package = self.export.package
        write_int(writer, self.internal_version)
        write_index(writer, len(self.ref_bones))
        for bone in self.ref_bones:
            bone.serialize(writer, package)
        write_index(writer, len(self.moves))
        for chunk in self.moves:
            chunk.serialize(writer, stream_position)
        write_index(writer, len(self.anim_seqs))
        for seq in self.anim_seqs:
            seq.serialize(writer, package)

    def to_dict(self) -> Dict[str, Any]:
        """Return the animation set's dict representation.

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        d["internal_version"] = self.internal_version
        d["ref_bones"] = [bone.to_dict(self) for bone in self.ref_bones]
        d["moves"] = [chunk.to_dict() for chunk in self.moves]
        d["anim_seqs"] = [seq.to_dict(self) for seq in self.anim_seqs]
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the animation set from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        self.internal_version = _as_int(data.get("internal_version"))
        self.ref_bones = []
        for value in _as_list(data.get("ref_bones")):
            bone = _UnNamedBone()
            bone.from_dict(value, self)
            self.ref_bones.append(bone)
        self.moves = []
        for value in _as_list(data.get("moves")):
            chunk = _UnMotionChunk()
            chunk.from_dict(value)
            self.moves.append(chunk)
        self.anim_seqs = []
        for value in _as_list(data.get("anim_seqs")):
            seq = _UnMeshAnimSeq()
            seq.from_dict(value, self)
            self.anim_seqs.append(seq)


class _UnNamedBone:
    """One bone of an animation set's own skeleton.

    Layout::

        <name : name ref> <flags : uint32> <parent index : int32>
    """

    def __init__(self) -> None:
        """Initialise an unnamed root bone."""
        self.name = _UnNameRef()
        self.flags: int = 0
        self.parent_index: int = 0

    def _name_refs(self):
        """Yield the bone's name reference.

        Yields:
            _UnNameRef: The bone name.
        """
        yield self.name

    def parse(self, reader: BinaryIO, package: "UnPackage") -> None:
        """Parse the bone.

        Args:
            reader (BinaryIO): Stream positioned at the bone name.
            package ("UnPackage"): The owning package.
        """
        self.name.parse(reader, package)
        self.flags = read_uint(reader)
        self.parent_index = read_int(reader)

    def serialize(self, writer: BinaryIO, package: "UnPackage") -> None:
        """Serialise the bone.

        Args:
            writer (BinaryIO): Stream to write to.
            package ("UnPackage"): The owning package.
        """
        self.name.serialize(writer, package)
        write_uint(writer, self.flags)
        write_int(writer, self.parent_index)

    def to_dict(self, owner: "UnObject") -> Dict[str, Any]:
        """Return the bone's dict representation.

        Args:
            owner ("UnObject"): The object that owns this bone.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "name": self.name.to_str(owner),
            "flags": self.flags,
            "parent_index": self.parent_index,
        }

    def from_dict(self, data: Any, owner: "UnObject") -> None:
        """Populate the bone from its dict representation.

        Args:
            data (Any): The transport representation.
            owner ("UnObject"): The object that owns this bone.
        """
        src = data if isinstance(data, dict) else {}
        self.name.from_str(owner, src.get("name"))
        self.flags = _as_int(src.get("flags"))
        self.parent_index = _as_int(src.get("parent_index"))


class _UnUvStream:
    """One texture-coordinate stream of a static mesh.

    Layout::

        <UV pairs : 8 bytes each> <coordinate index : int32>
        <revision : int32>
    """

    def __init__(self) -> None:
        """Initialise an empty UV stream."""
        self.uvs = _UnRawArray(8)
        self.coordinate_index: int = 0
        self.revision: int = 0

    def parse(self, reader: BinaryIO) -> None:
        """Parse the UV stream.

        Args:
            reader (BinaryIO): Stream positioned at the UV pair count.
        """
        self.uvs.parse(reader)
        self.coordinate_index = read_int(reader)
        self.revision = read_int(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the UV stream.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        self.uvs.serialize(writer, stream_position)
        write_int(writer, self.coordinate_index)
        write_int(writer, self.revision)

    def to_dict(self) -> Dict[str, Any]:
        """Return the UV stream's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {
            "uvs": self.uvs.to_dict(),
            "coordinate_index": self.coordinate_index,
            "revision": self.revision,
        }

    def from_dict(self, data: Any) -> None:
        """Populate the UV stream from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.uvs.from_dict(src.get("uvs"))
        self.coordinate_index = _as_int(src.get("coordinate_index"))
        self.revision = _as_int(src.get("revision"))


class _UnKDopTree:
    """A static mesh's collision bounding-volume tree.

    Layout::

        <nodes : 32 bytes each> <triangles : 8 bytes each>
    """

    def __init__(self) -> None:
        """Initialise an empty tree."""
        self.nodes = _UnRawArray(32)
        self.triangles = _UnRawArray(8)

    def parse(self, reader: BinaryIO) -> None:
        """Parse the tree.

        Args:
            reader (BinaryIO): Stream positioned at the node count.
        """
        self.nodes.parse(reader)
        self.triangles.parse(reader)

    def serialize(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the tree.

        Args:
            writer (BinaryIO): Stream to write to.
            stream_position (int): Absolute position of the owning object.
        """
        self.nodes.serialize(writer, stream_position)
        self.triangles.serialize(writer, stream_position)

    def to_dict(self) -> Dict[str, Any]:
        """Return the tree's dict representation.

        Returns:
            Dict[str, Any]: The transport representation.
        """
        return {"nodes": self.nodes.to_dict(), "triangles": self.triangles.to_dict()}

    def from_dict(self, data: Any) -> None:
        """Populate the tree from its dict representation.

        Args:
            data (Any): The transport representation.
        """
        src = data if isinstance(data, dict) else {}
        self.nodes.from_dict(src.get("nodes"))
        self.triangles.from_dict(src.get("triangles"))


class UnStaticMesh(UnPrimitive):
    """A rigid, pre-triangulated mesh resource.

    Its material list and collision options are ordinary script properties, so
    they arrive in the tagged-property block.  After the primitive bounds
    comes::

        <sections : 14 bytes each> <bounding box : repeated>
        <vertex stream> <colour stream> <alpha stream> <UV streams>
        <index buffer> <wireframe index buffer>
        <collision model : object ref> <collision tree>
        <source triangles : lazy array> <internal version : int32>
        licensee version == 0x16: <smoothing threshold : float32>
        licensee version >  0x16: <max smoothing angles : 4 bytes each>
        package version >= 100: <karma properties : object ref>
        package version >= 120: <authentication key : uint32>

    Package versions below 112 (which store their render data per section) fall
    back to a verbatim payload.
    """

    def __init__(self, export: "UnExport") -> None:
        """Initialise an empty static mesh.

        Args:
            export ("UnExport"): The export entry that owns this mesh.
        """
        super().__init__(export)
        self.sections = _UnRawArray(14)
        self.sections_bounding_box: Dict[str, Any] = _empty_box()
        self.vertex_stream = _UnStream(24)
        self.color_stream = _UnStream(4)
        self.alpha_stream = _UnStream(4)
        self.uv_streams: List[_UnUvStream] = []
        self.index_buffer = _UnStream(2)
        self.wireframe_index_buffer = _UnStream(2)
        self.collision_model = _UnObjRef()
        self.kdop_tree = _UnKDopTree()
        self.raw_triangles = _UnStaticMeshTriangleArray()
        self.internal_version: int = 0
        self.smoothing_threshold: float = 0.0
        self.max_smoothing_angles = _UnRawArray(4)
        self.k_physics_props = _UnObjRef()
        self.authentication_key: int = 0

    def _object_refs(self):
        """Yield every object reference in this mesh.

        Yields:
            _UnObjRef: Each object reference, in layout order.
        """
        yield from super()._object_refs()
        yield self.collision_model
        yield self.k_physics_props

    def _parse_tail(self, reader: BinaryIO) -> None:
        """Parse the static mesh data.

        Args:
            reader (BinaryIO): Stream positioned just past the ``None`` name.

        Raises:
            ValueError: When the package predates the shared render streams.
        """
        super()._parse_tail(reader)
        package = self.export.package
        if package.version < 112:
            raise ValueError(f"unsupported package version {package.version}")
        self.sections.parse(reader)
        self.sections_bounding_box = _read_box(reader)
        self.vertex_stream.parse(reader)
        self.color_stream.parse(reader)
        self.alpha_stream.parse(reader)
        self.uv_streams = [_UnUvStream() for _ in range(_read_count(reader))]
        for stream in self.uv_streams:
            stream.parse(reader)
        self.index_buffer.parse(reader)
        self.wireframe_index_buffer.parse(reader)
        self.collision_model.parse(reader)
        self.kdop_tree.parse(reader)
        self.raw_triangles.parse(reader)
        self.internal_version = read_int(reader)
        if package.licensee_version == 0x16:
            self.smoothing_threshold = read_float(reader)
        elif package.licensee_version > 0x16:
            self.max_smoothing_angles.parse(reader)
        if package.version >= 100:
            self.k_physics_props.parse(reader)
        if package.version >= 120:
            self.authentication_key = read_uint(reader)

    def _serialize_tail(self, writer: BinaryIO, stream_position: int) -> None:
        """Serialise the static mesh data.

        Args:
            writer (BinaryIO): Stream positioned just past the ``None`` name.
            stream_position (int): Absolute position of the object in the
                output stream.
        """
        super()._serialize_tail(writer, stream_position)
        package = self.export.package
        self.sections.serialize(writer, stream_position)
        _write_box(writer, self.sections_bounding_box)
        self.vertex_stream.serialize(writer, stream_position)
        self.color_stream.serialize(writer, stream_position)
        self.alpha_stream.serialize(writer, stream_position)
        write_index(writer, len(self.uv_streams))
        for stream in self.uv_streams:
            stream.serialize(writer, stream_position)
        self.index_buffer.serialize(writer, stream_position)
        self.wireframe_index_buffer.serialize(writer, stream_position)
        self.collision_model.serialize(writer)
        self.kdop_tree.serialize(writer, stream_position)
        self.raw_triangles.serialize(writer, stream_position)
        write_int(writer, self.internal_version)
        if package.licensee_version == 0x16:
            write_float(writer, self.smoothing_threshold)
        elif package.licensee_version > 0x16:
            self.max_smoothing_angles.serialize(writer, stream_position)
        if package.version >= 100:
            self.k_physics_props.serialize(writer)
        if package.version >= 120:
            write_uint(writer, self.authentication_key)

    def to_dict(self) -> Dict[str, Any]:
        """Return the static mesh's dict representation.

        Returns:
            Dict[str, Any]: The serialisable dict representation.
        """
        d = super().to_dict()
        d["sections"] = self.sections.to_dict()
        d["sections_bounding_box"] = _box_to_dict(self.sections_bounding_box)
        d["vertex_stream"] = self.vertex_stream.to_dict()
        d["color_stream"] = self.color_stream.to_dict()
        d["alpha_stream"] = self.alpha_stream.to_dict()
        d["uv_streams"] = [stream.to_dict() for stream in self.uv_streams]
        d["index_buffer"] = self.index_buffer.to_dict()
        d["wireframe_index_buffer"] = self.wireframe_index_buffer.to_dict()
        d["collision_model"] = self.collision_model.to_str(self)
        d["kdop_tree"] = self.kdop_tree.to_dict()
        d["raw_triangles"] = self.raw_triangles.to_dict()
        d["internal_version"] = self.internal_version
        d["smoothing_threshold"] = format_float32(self.smoothing_threshold)
        d["max_smoothing_angles"] = self.max_smoothing_angles.to_dict()
        d["k_physics_props"] = self.k_physics_props.to_str(self)
        d["authentication_key"] = self.authentication_key
        return d

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Populate the static mesh from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to load from.
        """
        super().from_dict(data)
        self.sections.from_dict(data.get("sections"))
        self.sections_bounding_box = _box_from_dict(data.get("sections_bounding_box"))
        self.vertex_stream.from_dict(data.get("vertex_stream"))
        self.color_stream.from_dict(data.get("color_stream"))
        self.alpha_stream.from_dict(data.get("alpha_stream"))
        self.uv_streams = []
        for value in _as_list(data.get("uv_streams")):
            stream = _UnUvStream()
            stream.from_dict(value)
            self.uv_streams.append(stream)
        self.index_buffer.from_dict(data.get("index_buffer"))
        self.wireframe_index_buffer.from_dict(data.get("wireframe_index_buffer"))
        self.collision_model.from_str(self, data.get("collision_model"))
        self.kdop_tree.from_dict(data.get("kdop_tree"))
        self.raw_triangles.from_dict(data.get("raw_triangles"))
        self.internal_version = _as_int(data.get("internal_version"))
        self.smoothing_threshold = _as_float(data.get("smoothing_threshold"))
        self.max_smoothing_angles.from_dict(data.get("max_smoothing_angles"))
        self.k_physics_props.from_str(self, data.get("k_physics_props"))
        self.authentication_key = _as_int(data.get("authentication_key"))


def build_mesh_object(
    mesh_class: Type["UnObject"], export: "UnExport"
) -> Optional["UnObject"]:
    """Return a structured mesh object for *export*, or ``None``.

    A mesh resource carries a long native tail whose layout depends on both the
    package version and the resource's own internal version.  The structured
    object is only accepted when re-serialising the parse reproduces the
    export's bytes exactly; an unmodelled variant therefore degrades to a
    verbatim raw blob instead of failing the whole package load.

    Args:
        mesh_class (Type[UnObject]): The mesh type to instantiate.
        export (UnExport): The export to structure.

    Returns:
        Optional[UnObject]: The parsed mesh, or ``None`` when its payload does
            not round-trip byte-for-byte.
    """
    obj = mesh_class(export)
    data = export.export_data
    if not data:
        # No bytes to validate against — an XML import supplies them later.
        return obj
    try:
        obj._parse(io.BytesIO(data))
        check = io.BytesIO()
        obj._serialize(check, max(export.original_data_offset, 0))
        if check.getvalue() == data:
            return obj
    except Exception:
        pass
    return None


# ===================================================================== #
#  Object factory
# ===================================================================== #

_CLASS_NAME_MAP: Dict[str, Callable[["UnExport"], "UnObject"]] = {
    "Core.Class": UnClass,
    "Core.Enum": UnEnum,
    "Core.Struct": UnStruct,
    "Core.State": UnState,
    "Core.Function": UnFunction,
    "Core.Const": UnConst,
    "Core.TextBuffer": UnTextBuffer,
    "Core.ByteProperty": UnByteProperty,
    "Core.IntProperty": UnIntProperty,
    "Core.BoolProperty": UnBoolProperty,
    "Core.FloatProperty": UnFloatProperty,
    "Core.ObjectProperty": UnObjectProperty,
    "Core.ClassProperty": UnClassProperty,
    "Core.NameProperty": UnNameProperty,
    "Core.StrProperty": UnStrProperty,
    "Core.FixedArrayProperty": UnFixedArrayProperty,
    "Core.ArrayProperty": UnArrayProperty,
    "Core.MapProperty": UnMapProperty,
    "Core.StructProperty": UnStructProperty,
    "Core.DelegateProperty": UnDelegateProperty,
    "Core.PointerProperty": UnPointerProperty,
}


# Content (non-code) object classes.  These carry engine data (textures,
# sounds, materials) rather than script, so importing one from a dependency
# does not make that package *code*: it can still be replaced with a
# placeholder.  They are kept separate from ``_CLASS_NAME_MAP`` above so they
# never leak into ``CODE_CLASS_NAMES``, but are merged into the factory below.
_CONTENT_CLASS_NAME_MAP = {
    "Engine.Texture": UnTexture,
    "Engine.Sound": UnSound,
    "Engine.ConstantColor": UnConstantColor,
    "Engine.Combiner": UnCombiner,
    "Engine.Shader": UnShader,
    "Engine.FinalBlend": UnFinalBlend,
}


# Mesh resources.  Also content, but built through
# :func:`build_mesh_object` rather than instantiated directly, so an
# unmodelled layout variant can fall back to a verbatim raw blob.  Kept out of
# ``_CLASS_NAME_MAP`` for that reason; ``create_object`` checks this map first.
_MESH_CLASS_NAME_MAP: Dict[str, Type["UnObject"]] = {
    "Engine.StaticMesh": UnStaticMesh,
    "Engine.SkeletalMesh": UnSkeletalMesh,
    "Engine.MeshAnimation": UnMeshAnimation,
}


# Short (unqualified) names of every *code* class this module can parse.
# Importing any of these from a dependency package means that package
# contributes *code* (a Class/Struct/Function/State/Enum/Const or a
# property field) which must be resolved — so it cannot be replaced with a
# placeholder.  Everything else (Texture, Sound, meshes, material modifiers,
# …) is content and safe to placeholder when the source package is absent.
CODE_CLASS_NAMES = frozenset(
    class_name.split(".", 1)[-1] for class_name in _CLASS_NAME_MAP
)


# The factory dispatches on the fully-qualified class name across both code
# and content classes.
_CLASS_NAME_MAP.update(_CONTENT_CLASS_NAME_MAP)


class UnDefaultObject(UnObject):
    """Generic fallback object for exports whose class has no dedicated type.

    Parses the standard object payload (optional state frame + tagged
    properties) so an embedded subobject/component (e.g. a ``Shader`` or
    ``GUIButton`` instance) exposes its default values for defaultproperties
    reconstruction. It is intentionally NOT registered in the object factory:
    it is created on demand by the decompiler and never assigned to
    ``export.object``, so it can never re-serialize (and thus never truncate)
    the raw bytes of objects that carry binary data after their tagged
    properties (textures, sounds, meshes, …).
    """

    def dump(self) -> str:
        """Return a one-line summary of the generic object.

        Returns:
            str: A description with the class name and tagged-property count.
        """
        return (
            f"UnDefaultObject({self.export.object_name_string}, "
            f"{self.export.class_name_string}, "
            f"{len(self.tagged_properties)} tagged props)"
        )


def create_object(export: "UnExport") -> Optional["UnObject"]:
    """Create the appropriate object type for *export* based on its class name.

    Returns ``None`` if the class is not recognised, or when a mesh resource's
    payload does not round-trip byte-for-byte through the structured parse.
    """
    mesh_class = _MESH_CLASS_NAME_MAP.get(export.class_name_string)
    if mesh_class is not None:
        return build_mesh_object(mesh_class, export)
    cls = _CLASS_NAME_MAP.get(export.class_name_string)
    if cls is None:
        return None
    return cls(export)


class UnComponentObject(UnObject):
    """Generic property-container object for an embedded component/subobject.

    Some exports are instances of classes the tool does not model (e.g. GUI
    widgets ``GUIButton``/``GUILabel``/``moCheckBox`` embedded in a dialog).
    Their payload is a plain tagged-property list — like defaultproperties —
    with no class-specific native data after it. This type parses that list so
    XML export can emit the properties inline (as ``<ObjectData>``) instead of
    dumping an opaque ``Raw<Class>/*.bin`` blob.

    It is **not** registered in the object factory: for a normal ``.u`` load
    ``export.object`` stays ``None`` for these classes (so the raw bytes — and
    their obfuscation/name-remap handling — are untouched). It is built on
    demand by the XML exporter via :func:`build_component_object`, which only
    accepts a *pure* property container that round-trips byte-exactly, and is
    re-created by the XML importer when it meets a ``UnComponentObject``
    ``<ObjectData>`` whose class the factory cannot resolve.
    """

    def dump(self) -> str:
        """Return a one-line summary of the component object.

        Returns:
            str: A description with the class name and tagged-property count.
        """
        return (
            f"UnComponentObject({self.export.object_name_string}, "
            f"{self.export.class_name_string}, "
            f"{len(self.tagged_properties)} tagged props)"
        )


def build_component_object(export: "UnExport") -> Optional["UnComponentObject"]:
    """Return a structured component object for *export*, or ``None``.

    Only succeeds when the export's raw bytes are a *pure* tagged-property
    container that reconstructs byte-exactly through the XML transport:

    1. the property list parses and is followed by **no** trailing bytes (a
       class with native data after its properties — a texture, mesh, … —
       fails here and is kept as a raw blob);
    2. re-serialising the parsed properties reproduces the original bytes; and
    3. a full ``to_dict()`` -> ``from_dict()`` -> serialise round-trip also
       reproduces them (so XML export/import is guaranteed lossless).

    Args:
        export (UnExport): The (object-less) export to try to structure.

    Returns:
        Optional[UnComponentObject]: The parsed container, or ``None`` when the
            export is not a losslessly round-trippable pure property container.
    """
    data = export.export_data
    if not data:
        return None
    obj = UnComponentObject(export)
    try:
        mem = io.BytesIO(data)
        obj._parse(mem)
        trailing = mem.read()
    except Exception:
        return None
    if trailing:
        return None  # native data after the property list — not a pure container
    try:
        low = io.BytesIO()
        obj._serialize(low, 0)
        if low.getvalue() != data:
            return None
        # Verify the XML transport path (dict round-trip) is lossless too.
        clone = UnComponentObject(export)
        clone.from_dict(obj.to_dict())
        rt = io.BytesIO()
        clone._serialize(rt, 0)
        if rt.getvalue() != data:
            return None
    except Exception:
        return None
    return obj


# Register this module's factory with ``package.py`` so that
# :meth:`UnExport.create_object` can dispatch to it without importing this
# module (which would create a circular dependency).
register_object_factory(create_object)
