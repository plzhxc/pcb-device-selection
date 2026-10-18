"""Offline, condition-aware application-fragment research APIs.

Source annotations are caller supplied. Successful instantiation establishes
only a partial connectivity construction, never manufacturer authentication or
whole-board electrical correctness.
"""

from .applicability import assess
from .instantiate import compile_application, instantiate_fragment
from .models import Fragment, Target, canonical_digest

__all__ = ["Fragment", "Target", "assess", "canonical_digest",
           "instantiate_fragment", "compile_application"]
