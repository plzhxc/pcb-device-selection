"""Public reference-netlist format adaptation API."""

from validation_platform.reference.models import (
    ReferenceCapability,
    ReferenceCapabilityError,
    ReferenceLoadResult,
    ReferenceProvenance,
    ReferenceSource,
    UnifiedNetlist,
)
from validation_platform.reference.registry import (
    ReferenceFormatError,
    available_reference_formats,
    load_reference,
    load_reference_dir,
)

__all__ = [
    "ReferenceCapability",
    "ReferenceCapabilityError",
    "ReferenceFormatError",
    "ReferenceLoadResult",
    "ReferenceProvenance",
    "ReferenceSource",
    "UnifiedNetlist",
    "available_reference_formats",
    "load_reference",
    "load_reference_dir",
]
