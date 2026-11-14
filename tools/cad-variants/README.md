# CAD editions of claude-for-manufacturing

`plugins/manufacturing-solidworks` and `plugins/manufacturing-onshape` are built
from `plugins/claude-for-manufacturing` by `build.py`. Don't edit them by hand:
edit the overlays or the text edits here, then rebuild.

```
python3 tools/cad-variants/build.py            # rebuild both editions
python3 tools/cad-variants/build.py --check    # fail if the committed editions are stale
python3 -I -m unittest discover -s tools/cad-variants/tests
```

| Path | What it is |
|---|---|
| `build.py` | Copies the upstream plugin, applies the overlays, then exact-text edits wherever upstream names Fusion. Each edit must match once, so an upstream change stops the build instead of shipping a half-converted plugin. Fails if Fusion is still named anywhere it shouldn't be |
| `common/mcp_stdio.py` | A standard-library MCP server over stdio, shared by both bridges |
| `overlays/solidworks/` | Manifest, `.mcp.json`, the SOLIDWORKS bridge (COM, via pywin32) and its CAD adapter |
| `overlays/onshape/` | Manifest, `.mcp.json`, the Onshape bridge (REST, signed API keys) and its CAD adapter |
| `NOTICE` | Apache-2.0 attribution, copied into each edition |
| `tests/` | MCP framing, Onshape request signing, helpers, and every write guard against fake CAD sessions |

## What is and isn't verified

- **Verified here:** the build, both plugins with `claude plugin validate` and the
  repository's own folder, frontmatter and licence checks, and the bridges'
  logic and guards against fake SOLIDWORKS and Onshape sessions.
- **Not verified:** either bridge against the real thing. SOLIDWORKS method names
  and enums were checked against a mirror of the SOLIDWORKS API Help; Onshape
  endpoints against Onshape's API documentation and its generated Go client.
  Neither bridge has talked to a live SOLIDWORKS session or Onshape account.
- **Built, unverified:** creating models from drawings or text (path C2/A: sketch,
  extrude, cut, fillet, chamfer), STEP import (C1), and listing bodies and external
  references for the integrity check. Each build step is checked and undone on
  failure; the SOLIDWORKS default part template enum (8) is not confirmed.
- **Not built:** CAM. Neither SOLIDWORKS CAM nor Onshape CAM Studio has a published
  API, so both editions use the feature path, which upstream already defines, and
  say so. Threads, lofts and sheet metal are hand-entered beside the model.

## First live run

SOLIDWORKS: on Windows with SOLIDWORKS open on a throwaway part, `pip install pywin32`,
start Claude with `--plugin-dir plugins/manufacturing-solidworks`, and ask for
`sw_status`, then `sw_read_parameters`, then a two-variant sweep into a scratch folder.
Then `sw_new_part` in a scratch folder and one sketch and extrude with `sw_add_feature`.

Onshape: make a read-only API key first, set `ONSHAPE_ACCESS_KEY` and
`ONSHAPE_SECRET_KEY`, and read a public or scratch Part Studio (`onshape_status`,
`onshape_read_parameters`, `onshape_read_properties`). Only then a read-write key
and a copied-document sweep, then `onshape_create_document` with one sketch and
extrude. Each call counts against the plan's annual API limit.
