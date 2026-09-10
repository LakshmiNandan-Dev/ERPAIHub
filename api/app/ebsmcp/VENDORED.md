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
