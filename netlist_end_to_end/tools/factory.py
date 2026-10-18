"""Toolbox assembly and role grants for standard knowledge access."""

from __future__ import annotations

from agent_framework.tool import ToolBox

from .batch import BatchDeviceEvidenceTool
from .config import KnowledgeConfig
from .device_knowledge import CatalogMilvusDeviceKnowledge, DeviceKnowledgeFetcher
from .milvus import MilvusDatasheetProvider
from .netlist_evidence import (
    CatalogMilvusNetlistKnowledgePrefetcher,
    NetlistKnowledgePrefetcher,
)
from .sql import MySQLDeviceCatalogProvider


def create_batch_device_evidence_toolbox(config: KnowledgeConfig) -> ToolBox:
    """Create one bounded SQL/Milvus batch-verification tool."""

    catalog = MySQLDeviceCatalogProvider(config)
    datasheets = MilvusDatasheetProvider(config, catalog)
    return ToolBox([BatchDeviceEvidenceTool(catalog, datasheets)])


def create_device_resolver_toolbox(config: KnowledgeConfig) -> ToolBox:
    """Create the single bounded batch-read tool used by DeviceResolver."""

    return create_batch_device_evidence_toolbox(config)


def create_netlist_knowledge_prefetcher(
    config: KnowledgeConfig,
) -> NetlistKnowledgePrefetcher:
    """Create the deterministic SQL/Milvus dependency used by netlist stages."""

    catalog = MySQLDeviceCatalogProvider(config)
    datasheets = MilvusDatasheetProvider(config, catalog)
    return CatalogMilvusNetlistKnowledgePrefetcher(catalog, datasheets)


def create_device_knowledge_fetcher(config: KnowledgeConfig) -> DeviceKnowledgeFetcher:
    """Create the deterministic SQL/Milvus parameter pull used by requirement_intent."""

    catalog = MySQLDeviceCatalogProvider(config)
    datasheets = MilvusDatasheetProvider(config, catalog)
    return CatalogMilvusDeviceKnowledge(catalog, datasheets)


def knowledge_health(config: KnowledgeConfig) -> dict[str, object]:
    """Probe both backends without returning credentials."""

    catalog = MySQLDeviceCatalogProvider(config)
    datasheets = MilvusDatasheetProvider(config, catalog)
    sql = catalog.status()
    milvus = datasheets.status()
    return {
        "ok": bool(sql.get("available")) and bool(milvus.get("available")),
        "config": config.public_config(),
        "sql": sql,
        "milvus": milvus,
    }


__all__ = [
    "create_batch_device_evidence_toolbox",
    "create_device_knowledge_fetcher",
    "create_device_resolver_toolbox",
    "create_netlist_knowledge_prefetcher",
    "knowledge_health",
]
