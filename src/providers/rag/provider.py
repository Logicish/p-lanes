# providers/rag/provider.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/3/2026
#
# ==================================================
# RAG provider — ChromaDB persistent client wrapper.
#
# Collections (created on first use):
#   shared_home, shared_cooking, shared_electronics,
#   shared_games, shared_general  — accessible to all
#   user_root, user_marilyn, user_jj  — private per user
#
# All collections use cosine similarity (HNSW).
# Embeddings are always supplied externally (BGE-M3).
#
# Ingestor runs as a background asyncio task on start().
# trigger_ingest() lets the admin endpoint re-run it.
#
# ChromaDB sync calls run in a thread executor so the
# event loop is never blocked.
#
# Self-contained: reads providers/rag/config.yaml.
# Knows about: providers (registry), providers.base only.
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
from pathlib import Path

import structlog
import yaml

from providers.base import Provider

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Known collections — created lazily on first access
_SHARED_FOLDERS = ["home", "cooking", "electronics", "games", "general"]
_USER_IDS       = ["root", "marilyn", "jj"]

ALL_SHARED_COLLECTIONS = [f"shared_{f}" for f in _SHARED_FOLDERS]
ALL_USER_COLLECTIONS   = [f"user_{u}" for u in _USER_IDS]
ALL_COLLECTIONS        = ALL_SHARED_COLLECTIONS + ALL_USER_COLLECTIONS


# ==================================================
# RagProvider
# ==================================================

class RagProvider(Provider):

    def __init__(self, cfg: dict):
        self.chroma_path:       str  = cfg.get("chroma_path", "/var/lib/p-lanes/chroma")
        self.rag_path:          str  = cfg.get("rag_path",    "/var/lib/p-lanes/rag")
        self._top_k:            int  = cfg.get("top_k",       4)
        self._ingest_start:     bool = cfg.get("ingest_on_start", True)
        self.distance_threshold: float = cfg.get("distance_threshold", 0.35)
        self.collection_routing: dict  = cfg.get("collection_routing", {})
        self._client              = None
        self._collections:   dict = {}
        self._ready:         bool = False
        self._ingest_task         = None

    # --------------------------------------------------
    # Provider identity / state
    # --------------------------------------------------

    @property
    def name(self) -> str:
        return "rag"

    @property
    def is_ready(self) -> bool:
        return self._ready

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    async def start(self) -> bool:
        try:
            import chromadb
            loop = asyncio.get_event_loop()
            self._client = await loop.run_in_executor(
                None,
                lambda: chromadb.PersistentClient(path=self.chroma_path),
            )
            # Pre-warm all known collections
            await loop.run_in_executor(None, self._init_collections)

            self._ready = True
            log.info("rag_ready",
                     chroma_path=self.chroma_path,
                     rag_path=self.rag_path)

            if self._ingest_start:
                self._ingest_task = asyncio.create_task(
                    self._run_ingestor(),
                    name="rag_ingestor",
                )

            return True

        except Exception as e:
            log.error("rag_start_failed", error=str(e))
            return False

    async def stop(self) -> None:
        self._ready = False
        if self._ingest_task and not self._ingest_task.done():
            self._ingest_task.cancel()
            try:
                await self._ingest_task
            except asyncio.CancelledError:
                pass
        self._client = None
        log.info("rag_stopped")

    # --------------------------------------------------
    # Internal helpers
    # --------------------------------------------------

    def _init_collections(self) -> None:
        """Create all known collections if they don't exist. Sync."""
        for name in ALL_COLLECTIONS:
            self._get_collection(name)

    def _get_collection(self, name: str):
        """Get or create a ChromaDB collection. Sync, thread-safe."""
        if name not in self._collections:
            self._collections[name] = self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collections[name]

    async def _run_ingestor(self) -> None:
        """Background task wrapper around scan_and_ingest."""
        try:
            from providers.rag.ingestor import scan_and_ingest
            log.info("rag_ingestor_start")
            await scan_and_ingest(self)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("rag_ingestor_error", error=str(e))

    # --------------------------------------------------
    # Search
    # --------------------------------------------------

    async def search(
        self,
        query_embedding: list[float],
        collection_names: list[str],
        n_results: int | None = None,
    ) -> list[dict]:
        """Search collections and return merged results sorted by distance.

        Args:
            query_embedding:  1024-dim float list (BGE-M3 normalized).
            collection_names: Which collections to search.
            n_results:        Results per collection (defaults to top_k).

        Returns:
            List of dicts: {doc, source_file, section, collection, distance}.
            Sorted by distance ascending (lower = more similar for cosine).
        """
        if not self._ready or self._client is None:
            return []

        k    = n_results or self._top_k
        loop = asyncio.get_event_loop()

        results = []
        for col_name in collection_names:
            try:
                col = self._get_collection(col_name)
                count = await loop.run_in_executor(None, col.count)
                if count == 0:
                    continue

                raw = await loop.run_in_executor(
                    None,
                    lambda c=col: c.query(
                        query_embeddings=[query_embedding],
                        n_results=min(k, count),
                        include=["documents", "metadatas", "distances"],
                    ),
                )

                docs      = raw.get("documents", [[]])[0]
                metas     = raw.get("metadatas",  [[]])[0]
                distances = raw.get("distances",  [[]])[0]

                for doc, meta, dist in zip(docs, metas, distances):
                    results.append({
                        "doc":         doc,
                        "source_file": meta.get("source_file", ""),
                        "section":     meta.get("section", ""),
                        "collection":  col_name,
                        "distance":    dist,
                    })

            except Exception as e:
                log.warning("rag_search_collection_error",
                            collection=col_name, error=str(e))

        results.sort(key=lambda r: r["distance"])
        return results[:k]

    # --------------------------------------------------
    # Upsert
    # --------------------------------------------------

    def upsert_batch(
        self,
        collection_name: str,
        ids:        list[str],
        embeddings: list[list[float]],
        documents:  list[str],
        metadatas:  list[dict],
    ) -> None:
        """Upsert a batch of documents synchronously.
        Called from the ingestor (which already runs async-safely).
        """
        col = self._get_collection(collection_name)
        col.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    # --------------------------------------------------
    # Admin
    # --------------------------------------------------

    async def trigger_ingest(self) -> None:
        """Cancel any running ingest and start a fresh one."""
        if self._ingest_task and not self._ingest_task.done():
            self._ingest_task.cancel()
            try:
                await self._ingest_task
            except asyncio.CancelledError:
                pass

        self._ingest_task = asyncio.create_task(
            self._run_ingestor(),
            name="rag_ingestor",
        )
        log.info("rag_ingest_triggered")
