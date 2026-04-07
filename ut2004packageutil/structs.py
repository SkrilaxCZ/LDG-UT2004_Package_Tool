"""Core data structures for Unreal packages."""

import io as _io
from dataclasses import dataclass
from typing import Any, BinaryIO, List

from ut2004packageutil.utils.io_utils import read_index, write_index
from ut2004packageutil.utils.struct_utils import (
    read_byte,
    read_int,
    write_byte,
    write_int,
)


@dataclass
class Vector:
    """3D vector with X, Y, Z components."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass
class Rotator:
    """Rotation with Roll, Pitch, Yaw components."""

    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0


@dataclass
class GUID:
    """128-bit GUID stored as four 32-bit unsigned integers."""

    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0


class UnArray:
    """Compact-index count-prefixed raw byte array.

    Binary format: compact-index count + ``count`` raw bytes.
    """

    def __init__(self, data: bytes = b"") -> None:
        """Initialize the array with raw byte data.

        Args:
            data (bytes): The initial byte data. Defaults to b"".
        """
        self.data: bytes = data

    def parse(self, reader: BinaryIO) -> None:
        """Read a byte array from a binary stream.

        Args:
            reader (BinaryIO): The binary stream to read from.
        """
        count = read_index(reader)
        self.data = reader.read(count) if count > 0 else b""

    def serialize(self, writer: BinaryIO) -> None:
        """Write a byte array to a binary stream.

        Args:
            writer (BinaryIO): The binary stream to write to.
        """
        write_index(writer, len(self.data))
        if self.data:
            writer.write(self.data)

    def __len__(self) -> int:
        """Return the number of bytes in the array.

        Returns:
            int: The length of the byte data.
        """
        return len(self.data)

    def __repr__(self) -> str:
        """Return a debug representation of the array.

        Returns:
            str: A string showing the byte count.
        """
        return f"UnArray({len(self.data)} bytes)"


class UnTypedArray(UnArray):
    """Int-count-prefixed array with per-element serialisation.

    Binary format: 32-bit count followed by *count* elements, each
    serialised by the caller-provided *element_parse* / *element_serialize*
    callbacks.

    *element_type* is a string label for the element type (e.g. ``"BYTE"``,
    ``"TCHAR"``).
    """

    def __init__(self, element_type: str = "BYTE") -> None:
        """Initialize an empty typed array.

        Args:
            element_type (str): A string label for the element type
                (e.g. "BYTE", "TCHAR"). Defaults to "BYTE".
        """
        super().__init__()
        self.element_type: str = element_type
        self.items: List[Any] = []

    def parse(self, reader: BinaryIO, element_parse: Any = None) -> None:
        """Read a typed array from a binary stream.

        Args:
            reader (BinaryIO): The binary stream to read from.
            element_parse (Any): Called once per element with (reader) and
                must return the parsed element value. If None, reads raw
                bytes (one byte per element). Defaults to None.
        """
        count = read_int(reader)
        self.items = []
        if element_parse is None:
            for _ in range(count):
                self.items.append(read_byte(reader))
        else:
            for _ in range(count):
                self.items.append(element_parse(reader))

    def serialize(self, writer: BinaryIO, element_serialize: Any = None) -> None:
        """Write a typed array to a binary stream.

        Args:
            writer (BinaryIO): The binary stream to write to.
            element_serialize (Any): Called once per element with
                (writer, item). If None, writes raw bytes. Defaults to None.
        """
        write_int(writer, len(self.items))
        if element_serialize is None:
            for item in self.items:
                write_byte(writer, item)
        else:
            for item in self.items:
                element_serialize(writer, item)

    def __len__(self) -> int:
        """Return the number of elements in the array.

        Returns:
            int: The number of items.
        """
        return len(self.items)

    def __repr__(self) -> str:
        """Return a debug representation of the array.

        Returns:
            str: A string showing the element type and item count.
        """
        return f"UnTypedArray({self.element_type}, {len(self.items)} items)"


class UnString(UnTypedArray):
    """Unreal package string with ANSI/Unicode dual-encoding support.

    Serialisation:

    * Read: compact-index ``count``.  If ``count >= 0``, read *count*
      ANSI bytes (1 byte/char).  If ``count < 0``, read ``-count``
      Unicode chars (2 bytes/char, little-endian UTF-16).  Strip
      trailing NUL.
    * Write: emits as ANSI (positive count) with NUL terminator unless
      the string contains characters outside latin-1, in which case
      Unicode (negative count) is used.
    """

    def __init__(self, value: str = "") -> None:
        """Initialize the string with an optional value.

        Args:
            value (str): The initial string value. Defaults to "".
        """
        super().__init__(element_type="TCHAR")
        self.value: str = value

    def parse(self, reader: BinaryIO, element_parse: Any = None) -> None:
        """Read a string from a binary stream (ANSI or Unicode).

        Uses a compact-index count prefix.

        Args:
            reader (BinaryIO): The binary stream to read from.
            element_parse (Any): Unused; kept for signature compatibility.
                Defaults to None.
        """
        count = read_index(reader)
        if count == 0:
            self.value = ""
        elif count > 0:
            # ANSI string: count bytes (1 byte per char)
            raw = reader.read(count)
            self.value = raw.decode("latin-1").rstrip("\x00")
        else:
            # Unicode string: -count chars (2 bytes per char, UTF-16LE)
            char_count = -count
            raw = reader.read(char_count * 2)
            self.value = raw.decode("utf-16-le").rstrip("\x00")

    def serialize(self, writer: BinaryIO, element_serialize: Any = None) -> None:
        """Write a string to a binary stream (always ANSI).

        Uses a compact-index count prefix.

        Args:
            writer (BinaryIO): The binary stream to write to.
            element_serialize (Any): Unused; kept for signature
                compatibility. Defaults to None.
        """
        if not self.value:
            write_index(writer, 0)
        else:
            encoded = self.value.encode("latin-1") + b"\x00"
            write_index(writer, len(encoded))
            writer.write(encoded)

    def parse_from_bytes(self, data: bytes) -> None:
        """Parse a string from raw tagged property data bytes.

        Tagged property data uses compact-index length encoding.
        Handles both ANSI (positive count) and Unicode (negative count)
        strings.

        Args:
            data (bytes): The raw tagged property data to parse.
        """
        if not data:
            self.value = ""
            return
        reader = _io.BytesIO(data)
        count = read_index(reader)
        if count == 0:
            self.value = ""
        elif count > 0:
            raw = reader.read(count)
            try:
                self.value = raw.decode("latin-1").rstrip("\x00")
            except UnicodeDecodeError:
                self.value = ""
            self._is_unicode = False
        else:
            char_count = -count
            raw = reader.read(char_count * 2)
            try:
                self.value = raw.decode("utf-16-le").rstrip("\x00")
            except UnicodeDecodeError:
                self.value = ""
            self._is_unicode = True

    def serialize_to_bytes(self) -> bytes:
        """Serialize this string to raw tagged property data bytes.

        Tagged property data uses compact-index length encoding.
        Auto-selects Unicode encoding (negative count) when the string
        contains characters outside latin-1, or when ``_is_unicode`` was
        set by :meth:`parse_from_bytes`.

        Returns:
            bytes: The serialized tagged property data.
        """
        buf = _io.BytesIO()
        if not self.value:
            write_index(buf, 0)
            return buf.getvalue()

        use_unicode = getattr(self, "_is_unicode", False)
        if not use_unicode:
            try:
                self.value.encode("latin-1")
            except UnicodeEncodeError:
                use_unicode = True

        if use_unicode:
            encoded = self.value.encode("utf-16-le") + b"\x00\x00"
            char_count = len(encoded) // 2
            write_index(buf, -char_count)
            buf.write(encoded)
        else:
            encoded = self.value.encode("latin-1") + b"\x00"
            write_index(buf, len(encoded))
            buf.write(encoded)
        return buf.getvalue()

    def __repr__(self) -> str:
        """Return a debug representation of the string.

        Returns:
            str: A string showing the wrapped value.
        """
        return f"UnString({self.value!r})"

    def __str__(self) -> str:
        """Return the underlying string value.

        Returns:
            str: The wrapped string value.
        """
        return self.value

    def __eq__(self, other: object) -> bool:
        """Compare this string with another UnString or str.

        Args:
            other (object): The object to compare against.

        Returns:
            bool: True if the values are equal, False if not, or
                NotImplemented for unsupported types.
        """
        if isinstance(other, UnString):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other
        return NotImplemented
