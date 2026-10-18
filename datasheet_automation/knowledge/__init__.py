"""Datasheet knowledge store: Milvus text chunks + MySQL image / PDF bytes.

This package is the *writer* side of the datasheet knowledge base that the
generation flow (``netlist_end_to_end.tools.milvus``) and the Web Console read.
It replaces the externally-built ``DataSheetBase`` collection, whose image rows
often carried only a file path on another machine and no description.

Layout of the store (design: ``10_DB_SCHEMA/design/datasheet_knowledge_v2.md``):

* Milvus database ``DataSheetBaseV2`` / collection ``DataSheet`` — one row per
  chunk; text, table and image entries all carry retrievable ``content`` (for
  images: the VLM title / category / description).  The sparse BM25 vector is
  computed server-side by a Milvus ``Function`` from ``content``; no external
  embedding service is involved.
* MySQL database ``datasheet_assets`` — ``datasheet_documents`` (one row per
  C-number with the PDF bytes) and ``datasheet_images`` (one row per figure with
  the image bytes, keyed by SHA-256).  Milvus rows reference images by
  ``image_id``; nothing points at a filesystem path.

``store.KnowledgeStore.ingest_run`` loads one finished datasheet_automation
run directory (``opti_<C>_content_list.json`` + ``images/``); ``case_ingest``
drives the full pipeline for every core device of an evaluation case.
"""

from .store import KnowledgeStore, KnowledgeStoreConfig
from .preparation import (PreparationService, inventory_cases, inventory_functional_modules,
                          inventory_named_physical_devices, original_identity_metadata)
from .native_pdf import prepare_native_pdf
from .ocr_pdf import prepare_ocr_pdf
from .web_source import prepare_manufacturer_web

__all__ = ["KnowledgeStore", "KnowledgeStoreConfig", "PreparationService",
           "inventory_cases", "inventory_functional_modules", "inventory_named_physical_devices",
           "original_identity_metadata", "prepare_native_pdf",
           "prepare_ocr_pdf", "prepare_manufacturer_web"]
