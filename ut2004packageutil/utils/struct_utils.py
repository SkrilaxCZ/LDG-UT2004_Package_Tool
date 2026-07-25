"""Struct packing / unpacking helpers for binary I/O.

All functions use **little-endian** byte order, matching the Unreal Engine
binary format.
"""

import struct
from typing import BinaryIO

# ===================================================================== #
#  Pack helpers  (value → bytes)
# ===================================================================== #


def pack_byte(value: int) -> bytes:
    """Pack an unsigned 8-bit integer.

    Args:
        value (int): The value to pack.

    Returns:
        bytes: The 1-byte packed representation.
    """
    return struct.pack("B", value)


def pack_word(value: int) -> bytes:
    """Pack an unsigned 16-bit integer (little-endian).

    Args:
        value (int): The value to pack.

    Returns:
        bytes: The 2-byte little-endian packed representation.
    """
    return struct.pack("<H", value)


def pack_int(value: int) -> bytes:
    """Pack a signed 32-bit integer (little-endian).

    Args:
        value (int): The value to pack.

    Returns:
        bytes: The 4-byte little-endian packed representation.
    """
    return struct.pack("<i", value)


def pack_uint(value: int) -> bytes:
    """Pack an unsigned 32-bit integer (little-endian).

    Args:
        value (int): The value to pack.

    Returns:
        bytes: The 4-byte little-endian packed representation.
    """
    return struct.pack("<I", value)


def pack_ulong(value: int) -> bytes:
    """Pack an unsigned 64-bit integer (little-endian).

    Args:
        value (int): The value to pack.

    Returns:
        bytes: The 8-byte little-endian packed representation.
    """
    return struct.pack("<Q", value)


def pack_float(value: float) -> bytes:
    """Pack a 32-bit float (little-endian).

    Args:
        value (float): The value to pack.

    Returns:
        bytes: The 4-byte little-endian packed representation.
    """
    return struct.pack("<f", value)


def format_float32(value: float) -> str:
    """Render a float as the shortest decimal that round-trips through float32.

    UnrealScript stores floats as 32-bit IEEE values, but Python widens them to
    64-bit doubles on read, so ``str``/``repr`` prints the full double expansion
    of the widened value (e.g. ``0.2`` becomes ``0.20000000298023224``). This
    finds the shortest ``%g`` decimal that, when re-packed to float32, reproduces
    the exact same 32-bit value — i.e. the clean literal the author wrote. The
    result always carries a ``.`` (or exponent) so it reads as a float literal
    (``1`` -> ``1.0``); it re-encodes to identical bytes via :func:`pack_float`.

    Args:
        value (float): The (float32-widened) value to format.

    Returns:
        str: The shortest float32-round-tripping decimal literal.
    """
    if value != value:  # NaN
        return "0.0"
    if value in (float("inf"), float("-inf")):
        return repr(value)
    target = struct.unpack("<f", struct.pack("<f", value))[0]
    text = repr(target)
    for prec in range(1, 18):
        candidate = "%.*g" % (prec, target)
        if struct.unpack("<f", struct.pack("<f", float(candidate)))[0] == target:
            # Re-``repr`` the parsed value so normal-magnitude numbers avoid the
            # exponent form ``%g`` favours at low precision (``6e+01`` -> ``60.0``);
            # this round-trips to the identical double, hence the same float32.
            text = repr(float(candidate))
            break
    if "e" not in text and "E" not in text and "." not in text:
        text += ".0"
    return text


# ===================================================================== #
#  Unpack helpers  (bytes → value)
# ===================================================================== #


def unpack_byte(data: bytes) -> int:
    """Unpack an unsigned 8-bit integer.

    Args:
        data (bytes): The 1-byte buffer to unpack.

    Returns:
        int: The unpacked value.
    """
    return struct.unpack("B", data)[0]


def unpack_word(data: bytes) -> int:
    """Unpack an unsigned 16-bit integer (little-endian).

    Args:
        data (bytes): The 2-byte little-endian buffer to unpack.

    Returns:
        int: The unpacked value.
    """
    return struct.unpack("<H", data)[0]


def unpack_int(data: bytes) -> int:
    """Unpack a signed 32-bit integer (little-endian).

    Args:
        data (bytes): The 4-byte little-endian buffer to unpack.

    Returns:
        int: The unpacked value.
    """
    return struct.unpack("<i", data)[0]


def unpack_uint(data: bytes) -> int:
    """Unpack an unsigned 32-bit integer (little-endian).

    Args:
        data (bytes): The 4-byte little-endian buffer to unpack.

    Returns:
        int: The unpacked value.
    """
    return struct.unpack("<I", data)[0]


def unpack_ulong(data: bytes) -> int:
    """Unpack an unsigned 64-bit integer (little-endian).

    Args:
        data (bytes): The 8-byte little-endian buffer to unpack.

    Returns:
        int: The unpacked value.
    """
    return struct.unpack("<Q", data)[0]


def unpack_float(data: bytes) -> float:
    """Unpack a 32-bit float (little-endian).

    Args:
        data (bytes): The 4-byte little-endian buffer to unpack.

    Returns:
        float: The unpacked value.
    """
    return struct.unpack("<f", data)[0]


# ===================================================================== #
#  Stream read helpers  (BinaryIO → value)
# ===================================================================== #


def read_byte(reader: BinaryIO) -> int:
    """Read an unsigned 8-bit integer from a stream.

    Reads 1 byte from the stream.

    Args:
        reader (BinaryIO): The stream to read from.

    Returns:
        int: The value read.
    """
    return unpack_byte(reader.read(1))


def read_word(reader: BinaryIO) -> int:
    """Read an unsigned 16-bit integer (little-endian) from a stream.

    Reads 2 bytes from the stream.

    Args:
        reader (BinaryIO): The stream to read from.

    Returns:
        int: The value read.
    """
    return unpack_word(reader.read(2))


def read_int(reader: BinaryIO) -> int:
    """Read a signed 32-bit integer (little-endian) from a stream.

    Reads 4 bytes from the stream.

    Args:
        reader (BinaryIO): The stream to read from.

    Returns:
        int: The value read.
    """
    return unpack_int(reader.read(4))


def read_uint(reader: BinaryIO) -> int:
    """Read an unsigned 32-bit integer (little-endian) from a stream.

    Reads 4 bytes from the stream.

    Args:
        reader (BinaryIO): The stream to read from.

    Returns:
        int: The value read.
    """
    return unpack_uint(reader.read(4))


def read_ulong(reader: BinaryIO) -> int:
    """Read an unsigned 64-bit integer (little-endian) from a stream.

    Reads 8 bytes from the stream.

    Args:
        reader (BinaryIO): The stream to read from.

    Returns:
        int: The value read.
    """
    return unpack_ulong(reader.read(8))


def read_float(reader: BinaryIO) -> float:
    """Read a 32-bit float (little-endian) from a stream.

    Reads 4 bytes from the stream.

    Args:
        reader (BinaryIO): The stream to read from.

    Returns:
        float: The value read.
    """
    return unpack_float(reader.read(4))


# ===================================================================== #
#  Stream write helpers  (value → BinaryIO)
# ===================================================================== #


def write_byte(writer: BinaryIO, value: int) -> None:
    """Write an unsigned 8-bit integer to a stream.

    Writes 1 byte to the stream.

    Args:
        writer (BinaryIO): The stream to write to.
        value (int): The value to write.
    """
    writer.write(pack_byte(value))


def write_word(writer: BinaryIO, value: int) -> None:
    """Write an unsigned 16-bit integer (little-endian) to a stream.

    Writes 2 bytes to the stream.

    Args:
        writer (BinaryIO): The stream to write to.
        value (int): The value to write.
    """
    writer.write(pack_word(value))


def write_int(writer: BinaryIO, value: int) -> None:
    """Write a signed 32-bit integer (little-endian) to a stream.

    Writes 4 bytes to the stream.

    Args:
        writer (BinaryIO): The stream to write to.
        value (int): The value to write.
    """
    writer.write(pack_int(value))


def write_uint(writer: BinaryIO, value: int) -> None:
    """Write an unsigned 32-bit integer (little-endian) to a stream.

    Writes 4 bytes to the stream.

    Args:
        writer (BinaryIO): The stream to write to.
        value (int): The value to write.
    """
    writer.write(pack_uint(value))


def write_ulong(writer: BinaryIO, value: int) -> None:
    """Write an unsigned 64-bit integer (little-endian) to a stream.

    Writes 8 bytes to the stream.

    Args:
        writer (BinaryIO): The stream to write to.
        value (int): The value to write.
    """
    writer.write(pack_ulong(value))


def write_float(writer: BinaryIO, value: float) -> None:
    """Write a 32-bit float (little-endian) to a stream.

    Writes 4 bytes to the stream.

    Args:
        writer (BinaryIO): The stream to write to.
        value (float): The value to write.
    """
    writer.write(pack_float(value))
