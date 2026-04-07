"""Global package-loader singleton registry.

This module exists solely to hold the global :class:`PackageLoader`
instance.  By keeping it in its own module — with no dependencies on
``io``, ``object``, or ``package`` — both :mod:`ut2004packageutil.package.io`
and :mod:`ut2004packageutil.package.package_loader` can import it
without creating a circular dependency.
"""

from typing import Optional

_global_loader: Optional["PackageLoader"] = None


def get_package_loader() -> Optional["PackageLoader"]:
    """Return the global :class:`PackageLoader` singleton.

    Returns:
        Optional["PackageLoader"]: The registered loader, or None if no
            loader has been registered yet.
    """
    return _global_loader


def set_package_loader(loader: Optional["PackageLoader"]) -> None:
    """Set the global :class:`PackageLoader` singleton.

    Passing ``None`` clears the registration (mainly useful in tests).

    Args:
        loader (Optional["PackageLoader"]): The loader to register, or None
            to clear the registration.
    """
    global _global_loader
    _global_loader = loader
