# Final equivalence run (after the CTE materialization fix)

This is the §9 three-way HTTP replay of the full 544-specification corpus
(`../specs/`) plus 84 GET requests, re-run after commit 9afd1b4
("Materialize the year-pushdown CTE"), which fixed the only blocking defect
of the first run (population-071, a plan regression; see `../README.md`).

**Result: PASS — 544/544 specs and 84/84 GETs equivalent; 0 mismatches,
0 server errors.** Largest scaled SE/CI difference: 4.24e-15 (a vs b) and
2.79e-15 (b vs c), against the criterion bound 1e-12.

Code under test: the summary's "Code" column shows `283f819` because the
harness records `git rev-parse HEAD`; the run was started from a working tree
that already contained the (then uncommitted) fix, which was committed as
9afd1b4 immediately after launch with no further edits. The Phase 5 servers
import the package from that working tree (editable install), so targets b and
c ran the 9afd1b4 code.

- a: Phase 1-4 API (387fc2d) on the full database
- b: Phase 5 API on the full database
- c: Phase 5 API on the serving database restored from the release artifact
  (`0325:serving-20261020T224252Z-a7420a5c:de834e672474`), connected as the
  read-only `atus_api` role

Reproduce: `scripts/equivalence/run.sh --specs docs/evidence/phase5/equivalence/specs --out <dir> --serving-secrets-file <file> --jobs 1`.
