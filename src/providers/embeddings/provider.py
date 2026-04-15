# providers/embeddings/provider.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/3/2026
#
# ==================================================
# Embedding provider — wraps sentence-transformers
# with BGE-M3 running on GPU in fp16.
#
# BGE-M3 (BAAI/bge-m3) produces 1024-dim dense
# embeddings. Significantly better quality than
# e5-base for both semantic routing and RAG retrieval.
#
# Model is loaded once at startup and kept hot in
# VRAM (~1275 MiB in fp16). embed_async() runs in a
# thread executor to avoid blocking the event loop.
# Output vectors are cast to float32 for downstream
# numpy operations.
#
# Self-contained: reads providers/embeddings/config.yaml.
# Does not touch core config.
#
# Knows about: providers.base only.
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
from pathlib import Path

import numpy as np
import structlog
import torch
import yaml
from sentence_transformers import SentenceTransformer

from providers.base import EmbeddingProvider

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


# ==================================================
# EmbeddingsProvider
# ==================================================

class EmbeddingsProvider(EmbeddingProvider):

    def __init__(self, cfg: dict):
        self._model_name: str = cfg.get("model", "BAAI/bge-m3")
        self._device:     str = cfg.get("device", "cuda")
        self._dim:        int = cfg.get("dim", 1024)
        self._model:      SentenceTransformer | None = None
        self._ready:      bool = False

    # --------------------------------------------------
    # Provider identity / state
    # --------------------------------------------------

    @property
    def name(self) -> str:
        return "embeddings"

    @property
    def is_ready(self) -> bool:
        return self._ready

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    async def start(self) -> bool:
        try:
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None,
                lambda: SentenceTransformer(
                    self._model_name,
                    device=self._device,
                    model_kwargs={"torch_dtype": torch.float16},
                ),
            )

            # warmup — moves weights to device and runs first pass
            await self.embed_async(["warmup"])

            self._ready = True
            log.info("embeddings_ready",
                     model=self._model_name,
                     device=self._device,
                     dim=self._dim)
            return True

        except Exception as e:
            log.error("embeddings_start_failed", error=str(e))
            return False

    async def stop(self) -> None:
        self._ready = False
        self._model = None
        log.info("embeddings_stopped")

    # --------------------------------------------------
    # Embedding
    # --------------------------------------------------

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed texts synchronously. Blocks the caller.
        Use embed_async() from async contexts.
        Returns float32 ndarray of shape (N, dim)."""
        if self._model is None:
            raise RuntimeError("embeddings provider not started")
        vecs = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return vecs.astype(np.float32)

    async def embed_async(self, texts: list[str]) -> np.ndarray:
        """Embed texts in a thread executor.
        Safe to call from async pipeline modules.
        Returns float32 ndarray of shape (N, dim)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed, texts)
