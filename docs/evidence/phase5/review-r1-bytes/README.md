# Review round 1: byte counts behind the edge governor's estimates

Byte counts only; nothing here was timed. Taken on 2026-10-21 by the review's quota-safety reviewer against the local full database (read-only transactions), through a byte-counting TCP relay between the API and PostgreSQL. The relay counts plaintext protocol bytes; Neon adds TLS framing on top. Each analysis ran on a fresh API process after a warm-up request, so the per-process snapshot read is counted separately.

| File | What it holds |
|---|---|
| [`db_bytes_both_directions.txt`](db_bytes_both_directions.txt) | DB-to-API bytes (Neon egress) and API-to-DB bytes (Render outbound) per analysis, broad populations: the cold-process snapshot read (108,005 B), 1-, 7- and 22-year estimates, trends and compares, and one `422` decided before any statement (0 B) |
| [`rejected_422_egress.txt`](rejected_422_egress.txt) | Both directions for a narrow population (age 80-85, not in the labor force): lines 1-5 are `200` answers, four 22-year pooled estimates (up to 242,394 B, the figure ADR-015 and `EGRESS_ESTIMATE_BYTES` cite) and one 22-year compare (211,124 B); lines 6-8 are `422 insufficient_data` answers, which run the statistics query first (up to 376,495 B for a 22-year compare). Review round 2 corrected this index, which had filed the narrow-population estimates under the file above |
| [`meta_sizes.txt`](meta_sizes.txt) | Metadata response sizes from the real API, raw and gzipped, with header sizes (the lexicon: 70,851 B raw, ~8.5 KB gzipped) |

Used by: the per-analysis egress estimates in `edge/src/constants.ts` (`EGRESS_ESTIMATE_BYTES`), [ADR-015](../../../adr/ADR-015-edge-cache-snapshot-and-governor.md) ("Model error", "Render outbound through the Worker") and the Neon egress and Render outbound rows of [`deployment.md`](../../../deployment.md).
