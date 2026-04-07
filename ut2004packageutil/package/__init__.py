"""Package module for Unreal package data types.

Importing this package eagerly imports :mod:`object` for its side effect
of registering the :func:`UnExport.create_object` factory with
:mod:`package`.  Without this, code that uses only ``UnPackageIO``
directly (e.g. ``UnPackageIO().read_package(...)``) would end up with
exports whose ``object`` attribute is always ``None``.
"""

# noqa: F401 — imported solely for the object-factory side effect.
from ut2004packageutil.package import object as _object  # noqa: F401
