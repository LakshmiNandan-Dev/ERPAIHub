# Vendored from EBSMCP

This package is a copy of `mcp-server/src/ebsmcp` from the standalone
EBSMCP repository (github.com/LakshmiNandan-Dev/ERPMCP), modified to run as
a library inside OraEBSAgent.

- Source commit: bba3f1f
- Vendored: 2026-09-09

## Local modifications (do not exist upstream)
- Internal imports rewritten `ebsmcp.` -> `app.ebsmcp.`
- `context.py` added — the subject bridge to OraEBSAgent's auth
- `tools/registry.py` `current_subject()` reads the injected subject first

The upstream repo is the source of truth for tool logic. Re-vendor from it
rather than diverging tool code here; keep changes limited to the seams above.

### 4. SQL qualifiers: `APPLSYS.` / `AD.` -> `APPS.` (2026-09-10)

`api/app/ebsmcp/tools/dba/*.py` (7 files, 58 references) and the convention
docstring in `connectors/base.py`.

Found by running the catalog against a live R12.2 instance. Upstream EBSMCP
has the same defect — it is NOT fixed there, so re-vendoring will reintroduce
it. Three distinct failures, one cause:

* **Silent double-counting.** With Online Patching enabled, the `APPLSYS`
  base tables hold one row per edition. `APPLSYS.FND_CONCURRENT_PROGRAMS_TL`
  returned 53,214 rows against `APPS`'s 26,630, so every tool joining it
  reported each row twice — `concurrent_requests` returned 50 rows for 25
  requests, with a summary counting the duplicates. The `APPS` editioning
  views resolve to the running edition.
* **Objects in the wrong schema.** `APPLSYS.FND_USER_RESP_GROUPS` and
  `AD.AD_APPLIED_PATCHES` / `AD.AD_BUGS` raise ORA-00942 — there is no `AD`
  schema at all. `responsibility_assignments`, `sod_conflict_scan` and
  `patch_history` could never return a row.
* **Invented columns.** `patch_history`'s `applied_patches` view joined on
  `aap.bug_id` and selected `aap.status`; neither column exists. Rewritten
  against the real table, which needs no join — `PATCH_NAME` already holds
  the patch number.

`APPS` is the access path Oracle documents for application code, is uniform
across every product schema, and gives a read-only account a single grant
target.
