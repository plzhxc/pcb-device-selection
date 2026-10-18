"""Datasheet acquisition, parsing, and optimization automation.

The ``knowledge`` subpackage is the writer of the datasheet knowledge base
(Milvus ``DataSheetBaseV2`` chunks + MySQL ``datasheet_assets`` image / PDF
bytes); ``python -m datasheet_automation.main ingest-case --case <case>`` builds
it for every core device of an evaluation case.
"""

from .core.application import RESPONSE_SCHEMA, run

__all__ = ["RESPONSE_SCHEMA", "run"]
