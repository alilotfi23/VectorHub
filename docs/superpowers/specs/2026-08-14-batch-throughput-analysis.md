# Async Batch Throughput Analysis — 100k-Vector Jobs

**Date:** 2026-08-14
**Status:** Analysis complete. Conclusions **folded into `CLAUDE.md`** (batch note, commit `3c1e963`) — this document is the model and rationale behind them.
**Base specs:** `CLAUDE.md` (batch note); `docs/superpowers/specs/2026-08-14-vectorhub-platform-architecture-design.md` §6

## 1. Scope

Model the full async batch path for a 100k-vector job at 1536 dims (typical embedding dimension), with 4096 dims (the platform's `VECTOR_MAX_DIMENSION`) as the upper bound:

1. **Enqueue** (synchronous — the caller waits): client streams NDJSON → API stages to MinIO/S3 → `arq` enqueues `{job_id, object_key}`
2. **Job** (asynchronous): worker reads the object → parses/validates per line → chunked adapter upserts → writes per-vector results object

Verdict yardsticks (analysis targets, not platform SLOs): enqueue P95 < 30 s on localhost/LAN; job completion < 10 min for 100k × 1536-dim.

## 2. Assumptions

| Parameter | Value | Source / note |
|---|---|---|
| JSON bytes per float | ~10 B | `0.1234567,` — typical rendering of normalized floats |
| Record overhead | ~500 B | id + metadata + JSON structure |
| Dense dims | 1536 typical; 4096 platform max | `VECTOR_MAX_DIMENSION` |
| Client links | localhost, 1 Gbps LAN, 100 Mbps, 50 Mbps | |
| MinIO (local docker) | 1–2 GB/s single stream | NVMe, docker network |
| S3 | 100–300 MB/s per stream | multipart upload |
| Qdrant upsert | 10–30k pts/s @ 1536-dim, HNSW | conservative single-node; qdrant.tech ingestion guidance |
| Weaviate batch | 5–20k obj/s @ 1536-dim | conservative single-node; docs cite batch >100× single-add |
| Milvus insert | ~66k/s | published benchmark (HNSW, ~95% precision) |
| Chroma insert | ~12k/s, degrades at scale | third-party benchmark |

All backend rates assume self-hosted, single-node, moderate hardware, and are deliberately conservative. They are **estimates to be validated**, not facts — the Phase 3 soak test (consequence 3, §7) is the gate.

## 3. Payload sizing — the wire format dominates

JSON text is ~2.6× binary float32 (10 B/float vs 4 B/float):

| Payload | 100k × 1536-dim | 100k × 4096-dim |
|---|---|---|
| JSONL (v1 format) | **~1.6 GB** | ~4.3 GB |
| gzip'd JSONL | ~450 MB | ~1.2 GB |
| float32 binary (future format) | 614 MB | 1.6 GB |

A 100k × 1536-dim job's JSONL is the largest term in the entire batch path — bigger than every other stage combined. This is why the wire format, not the pipeline or the backends, sets the model's shape.

## 4. Enqueue path (streamed; memory stays flat)

Client → API upload, then API → object store. Never buffered whole; bounded memory regardless of job size.

| Link | Upload 1.6 GB | Total enqueue |
|---|---|---|
| localhost | ~1–2 s | ~3–5 s |
| 1 Gbps LAN | ~13 s | ~15–30 s |
| 100 Mbps client | ~2.2 min | minutes |
| 50 Mbps client | ~4.4 min | minutes — the pain point |

Enqueue is **bandwidth-bound, not CPU-bound**. The minutes-scale waits on slow client links are the quantified justification for the later-phase pre-signed direct-upload optimization (removes 1.6 GB from the API request path entirely).

## 5. Job path

| Stage | Time (100k × 1536-dim) |
|---|---|
| Read object (MinIO 1–2 GB/s; S3 100–300 MB/s) | 1–16 s |
| Parse + validate (json.loads + Pydantic, single-threaded) | ~7–15 s |
| **Upsert — Qdrant** (10–30k pts/s) | 3–10 s |
| **Upsert — Weaviate** (5–20k obj/s) | 5–20 s |
| **Upsert — Milvus** (~66k/s) | 1–3 s |
| **Upsert — Chroma** (~12k/s, degrades at scale) | **10–30 s** |
| Results object write (~5 MB) | < 1 s |

Pipeline-overlapped total: **~15–45 s worst case across all four backends**.

## 6. Verdict

- **Job time:** ~15–45 s vs the 10-min yardstick → 13–40× headroom. Even at the 4096-dim platform max the job stays under ~3 min.
- **Enqueue:** meets the < 30 s yardstick on localhost/LAN; misses it by an order of magnitude on slow client links — acceptable for v1, quantified rationale for the direct-upload later phase.
- **Bottleneck:** the wire format (1.6 GB JSONL), not the pipeline, not the backends. Raw JSONL meets targets as designed; compression is an optimization knob, not a requirement.
- **Floor:** Chroma is the throughput floor (10–30 s for 100k, degrading at scale) — the backend that defines the budget.

## 7. Design consequences (now normative in CLAUDE.md)

Three structural requirements, **required, not optional**:

1. **Chunked upsert is a first-class adapter contract** — `batch_upsert(chunk_size)` with per-backend sizing (Qdrant 5–10k/request, Weaviate ~1k with server-side batching, Milvus 1–10k, Chroma 100–1k) and backpressure. The worker never assumes one chunk size fits all.
2. **Bounded read→parse→upsert worker pipeline** — staged read-ahead queues so parse/validation (~7–15 s) overlaps with ingest; otherwise fast-ingest backends (Qdrant/Milvus) become parse-bound and their speed is wasted.
3. **Chroma soak test in Phase 3** — a 100k-vector ingest through `ChromaAdapter.batch_upsert` (adapter-level, since the full arq/MinIO path lands in Phase 6) validating the ~10–30 s budget and measuring Chroma's degradation curve; re-validated end-to-end in Phase 6.

**Later-phase knobs** (documented, not built): gzip payloads (stream-to-stream, memory-neutral, ~3–4× smaller); parallel/vectorized parsing; parquet for 1M+ loads; pre-signed direct-to-storage upload.

## 8. Validation plan

- **Phase 3 soak:** 100k × 1536-dim through `ChromaAdapter.batch_upsert`; assert the wall-time budget (expect 10–30 s) and record the degradation curve at 500k+ vectors.
- **Phase 6 end-to-end:** enqueue + job timing on the compose stack; verify pipeline overlap (parse hidden behind ingest) on Qdrant/Milvus; assert the bounded-memory property (streaming, bounded queues) at job runtime.
- **Knob benchmarks (when the knobs are built):** gzip vs raw enqueue time; parse parallelization speedup; chunk-size sensitivity per backend.

## 9. References

- Qdrant — large-scale ingestion guidance: <https://qdrant.tech/course/essentials/day-4/large-scale-ingestion/>
- Weaviate — batch import docs: <https://docs.weaviate.io/weaviate/manage-objects/import>
- Milvus — insert throughput figure (~66k/s, HNSW): <https://redis.io/blog/milvus-vs-redis-vector-database-comparison/>
- Chroma — insert throughput (~12k/s): <https://kanopylabs.com/blog/lancedb-vs-chroma-vs-sqlite-vec>
- Normative requirements: `CLAUDE.md` batch note (commit `3c1e963`)
