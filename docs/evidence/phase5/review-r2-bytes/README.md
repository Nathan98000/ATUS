# Review round 2: the size of the ungoverned `/health` answer

Byte counts only; nothing here was timed. Taken on 2026-10-21 for the review's "health-outbound-bound-understated" finding: the bound on Render outbound through the Worker's ungoverned `/api/health` assumed about half a kilobyte per answer, a figure no measurement supported.

| File | What it holds |
|---|---|
| [`health_bytes.py`](health_bytes.py) | The script, as run (`.venv/bin/python -I docs/evidence/phase5/review-r2-bytes/health_bytes.py`): the API app in production mode (`ATUS_ENV=production`, an edge secret, a 40-character `ATUS_BUILD_SHA`), served by uvicorn with the options `atus api` passes (`src/atus_pipeline/api/serve.py`: no `Server` header), on 127.0.0.1:9520. Its database URL points at a closed port; `/health` never touches a database, and nothing was contacted |
| [`health_bytes.txt`](health_bytes.txt) | Its output: each response header line with its size, and the total on the wire. 571 B (505 B of headers, a 66 B body), with and without the edge secret |

What it does not include: the headers Render's proxy adds or removes on the way out (`rndr-id`, `cf-ray` and the like), TLS framing, and HTTP/2 header compression between the Worker and Render. Whether Render counts headers toward outbound bandwidth is undocumented. The `Server-Timing` value and the request ID vary by a few bytes from answer to answer.

Used by: [ADR-015](../../../adr/ADR-015-edge-cache-snapshot-and-governor.md) ("Render outbound through the Worker") and the Render outbound row of [`deployment.md`](../../../deployment.md). At the Worker's whole 100,000 requests a day for 31 days, 571 B per answer is 1.77 GB; the bound leaves Render about 600 B per answer for its own headers before the worst case reaches the 5 GB.
