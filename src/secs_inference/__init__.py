"""Preserve the inference export without loading Torch for provider imports.

``SecsInference`` remains available from this package, but its model module is
loaded only when a caller asks for that established export. Provider processes
can therefore import their independent runtime without acquiring model resources.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from secs_inference.model import SecsInference


__all__ = ["SecsInference"]


def __getattr__(name: str):
    """Load the established inference export only when a caller requests it."""

    if name == "SecsInference":
        from secs_inference.model import SecsInference

        return SecsInference
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
