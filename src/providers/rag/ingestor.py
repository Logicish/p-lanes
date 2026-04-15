# providers/rag/ingestor.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/3/2026
#
# ==================================================
# RAG ingestor — scans /var/lib/p-lanes/rag/ for
# markdown files, chunks them by header, embeds via
# the BGE-M3 embeddings provider, and upserts into
# ChromaDB.
#
# Collection mapping:
#   rag/shared/{folder}/**/*.md → "shared_{folder}"
#   rag/users/{user_id}/**/*.md → "user_{user_id}"
#
# State tracking: chroma_path/ingest_state.json
#   Maps absolute file path → mtime float.
#   Only changed/new files are re-ingested.
#   Deleted file cleanup: docs with stale paths are
#   removed from the collection on next full scan.
#
# Chunk strategy:
#   Split on markdown ## (or deeper) headers.
#   Each chunk = header line + content until next header.
#   Pre-header content becomes "Introduction" chunk.
#   Empty chunks (whitespace only) are dropped.
#
# Knows about: providers (get_embedder only).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import json
import re
from pathlib import Path

import structlog

log = structlog.get_logger()

# ==================================================
# Constants
# ==================================================

_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)", re.MULTILINE)
_BATCH_SIZE = 32   # chunks per embed call


# ==================================================
# Collection name helpers
# ==================================================

def get_collection_name(rel_path: str) -> str | None:
    """Map a relative path (from rag_path) to a collection name.

    shared/home/recipes.md    → "shared_home"
    shared/cooking/pasta.md   → "shared_cooking"
    users/root/notes.md       → "user_root"

    Returns None if the path doesn't match the expected structure.
    """
    parts = Path(rel_path).parts
    if len(parts) < 2:
        return None
    top = parts[0]
    sub = parts[1]
    if top == "shared":
        return f"shared_{sub}"
    if top == "users":
        return f"user_{sub}"
    return None


# ==================================================
# Markdown chunker
# ==================================================

def chunk_markdown(text: str, source_file: str) -> list[dict]:
    """Split markdown text into header-bounded chunks.

    Returns list of dicts:
        {
            "section":  str,   # header text or "Introduction"
            "content":  str,   # full chunk text (header + body)
            "doc":      str,   # plain text for embedding (stripped)
        }
    """
    chunks = []
    matches = list(_HEADER_RE.finditer(text))

    if not matches:
        # No headers — whole file is one chunk
        stripped = text.strip()
        if stripped:
            chunks.append({
                "section": "Introduction",
                "content": stripped,
                "doc":     stripped,
            })
        return chunks

    # Content before first header
    pre = text[:matches[0].start()].strip()
    if pre:
        chunks.append({
            "section": "Introduction",
            "content": pre,
            "doc":     pre,
        })

    # Each header + content until next header
    for i, m in enumerate(matches):
        section    = m.group(1).strip()
        start      = m.start()
        end        = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw        = text[start:end].strip()
        body_only  = text[m.end():end].strip()
        if not body_only:
            continue
        chunks.append({
            "section": section,
            "content": raw,
            "doc":     body_only,
        })

    return chunks


# ==================================================
# State file helpers
# ==================================================

def _state_path(chroma_path: str) -> Path:
    return Path(chroma_path) / "ingest_state.json"


def _load_state(chroma_path: str) -> dict:
    p = _state_path(chroma_path)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(chroma_path: str, state: dict) -> None:
    p = _state_path(chroma_path)
    try:
        with open(p, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error("ingestor_state_save_failed", error=str(e))


# ==================================================
# Core ingest logic
# ==================================================

async def scan_and_ingest(provider) -> None:
    """Scan rag_path for markdown files and ingest changed ones.

    provider: RagProvider instance (has .chroma_path, .rag_path,
              ._get_collection(), .upsert_batch())

    Embeddings are requested via providers.get_embedder() so this
    module stays decoupled from the embeddings provider class.
    """
    import providers as prov_registry
    embedder = prov_registry.get_embedder()
    if embedder is None or not embedder.is_ready:
        log.error("ingestor_embedder_not_ready")
        return

    rag_root    = Path(provider.rag_path)
    chroma_path = provider.chroma_path
    state       = _load_state(chroma_path)
    new_state   = {}
    ingested    = 0
    skipped     = 0
    errors      = 0

    md_files = sorted(rag_root.rglob("*.md"))
    if not md_files:
        log.info("ingestor_no_files")
        return

    log.info("ingestor_scan_start", file_count=len(md_files))

    for filepath in md_files:
        rel      = str(filepath.relative_to(rag_root))
        col_name = get_collection_name(rel)
        if col_name is None:
            log.debug("ingestor_skip_unroutable", path=rel)
            continue

        mtime = filepath.stat().st_mtime
        new_state[str(filepath)] = mtime

        if state.get(str(filepath)) == mtime:
            skipped += 1
            continue

        try:
            text   = filepath.read_text(encoding="utf-8")
            chunks = chunk_markdown(text, rel)
            if not chunks:
                continue

            ids       = []
            docs      = []
            metadatas = []

            for idx, chunk in enumerate(chunks):
                doc_id = f"{col_name}::{rel}::{idx}"
                ids.append(doc_id)
                docs.append(chunk["doc"])
                metadatas.append({
                    "source_file": rel,
                    "section":     chunk["section"],
                    "collection":  col_name,
                })

            # Embed in batches
            all_vecs = []
            for i in range(0, len(docs), _BATCH_SIZE):
                batch = docs[i : i + _BATCH_SIZE]
                vecs  = await embedder.embed_async(batch)
                all_vecs.extend(vecs.tolist())

            # Delete old chunks for this file before upserting
            collection = provider._get_collection(col_name)
            try:
                existing = collection.get(where={"source_file": rel})
                if existing and existing["ids"]:
                    collection.delete(ids=existing["ids"])
            except Exception:
                pass  # collection may be empty — that's fine

            provider.upsert_batch(col_name, ids, all_vecs, docs, metadatas)
            ingested += 1
            log.debug("ingestor_file_ingested",
                      path=rel, chunks=len(chunks), collection=col_name)

        except Exception as e:
            errors += 1
            log.error("ingestor_file_failed", path=rel, error=str(e))

    _save_state(chroma_path, new_state)
    log.info("ingestor_complete",
             ingested=ingested, skipped=skipped, errors=errors)
