"""INI file parser with path normalization for Unreal Engine configuration files.

Handles the UT2004 INI format where keys can repeat (e.g. multiple ``Paths=``
lines).  Each key maps to a **list** of values.
"""

import os
from typing import Dict, List, Optional


def load_ini(path: str) -> Dict[str, Dict[str, List[str]]]:
    """Parse an INI file into sections with key → value-list mapping.

    Keys are case-sensitive.  Blank lines and ``; comment`` lines are ignored.

    Args:
        path (str): Path to the INI file to parse.

    Returns:
        Dict[str, Dict[str, List[str]]]: A mapping of
            ``{section_name: {key: [value1, value2, ...]}}``.
    """
    result: Dict[str, Dict[str, List[str]]] = {}
    current_section: Optional[str] = None

    with open(path, "r", encoding="latin-1") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                if current_section not in result:
                    result[current_section] = {}
                continue
            if current_section is None:
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                result[current_section].setdefault(key, []).append(value)

    return result


def get_section(
    ini_data: Dict[str, Dict[str, List[str]]],
    section_name: str,
) -> Dict[str, List[str]]:
    """Return a specific section from parsed INI data, or empty dict.

    Args:
        ini_data (Dict[str, Dict[str, List[str]]]): The parsed INI data.
        section_name (str): The name of the section to retrieve.

    Returns:
        Dict[str, List[str]]: The requested section, or an empty dict if the
            section is not present.
    """
    return ini_data.get(section_name, {})


def normalize_path(path: str) -> str:
    """Normalize path separators for the current OS.

    Converts backslashes to forward slashes on non-Windows platforms,
    and forward slashes to backslashes on Windows.

    Args:
        path (str): The path to normalize.

    Returns:
        str: The path with separators adjusted for the current OS.
    """
    if os.sep == "/":
        return path.replace("\\", "/")
    else:
        return path.replace("/", "\\")


def get_search_paths(ini_path: str) -> List[str]:
    """Extract and normalize search paths from ``[Core.System]`` ``Paths=``.

    Args:
        ini_path (str): Path to the INI file to read.

    Returns:
        List[str]: Directory glob patterns (e.g. ``../System/*.u``) with path
            separators fixed for the current OS.
    """
    ini_data = load_ini(ini_path)
    section = get_section(ini_data, "Core.System")
    raw_paths = section.get("Paths", [])
    return [normalize_path(p) for p in raw_paths]
