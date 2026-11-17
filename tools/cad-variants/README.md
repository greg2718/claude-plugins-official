# CAD editions of claude-for-manufacturing

`plugins/manufacturing-solidworks`, `plugins/manufacturing-onshape` and
`plugins/manufacturing-solidcraft` are built
from `plugins/claude-for-manufacturing` by `build.py`. Don't edit them by hand:
edit the overlays or the text edits here, then rebuild.

```
python3 tools/cad-variants/build.py            # rebuild all three editions
python3 tools/cad-variants/build.py --check    # fail if the committed editions are stale
python3 -I -m unittest discover -s tools/cad-variants/tests
# with SolidCraft installed, the live tests run too (skipped otherwise):
SOLIDCRAFT_CLI=/path/to/solidcraft-cli python3 -I -m unittest discover -s tools/cad-variants/tests -p 'test_solidcraft*.py' -v
```

| Path | What it is |
|---|---|
| `build.py` | Copies the upstream plugin, applies the overlays, then exact-text edits wherever upstream names Fusion. Each edit must match once, so an upstream change stops the build instead of shipping a half-converted plugin. Fails if Fusion is still named anywhere it shouldn't be |
| `common/mcp_stdio.py` | A standard-library MCP server over stdio, shared by the bridges |
| `overlays/solidworks/` | Manifest, `.mcp.json`, the SOLIDWORKS bridge (COM, via pywin32) and its CAD adapter |
| `overlays/onshape/` | Manifest, `.mcp.json`, the Onshape bridge (REST, signed API keys) and its CAD adapter |
| `overlays/solidcraft/` | Manifest, `.mcp.json`, the SolidCraft bridge (an MCP client to `solidcraft-cli mcp --headless`, with this plugin's guards on top) and its CAD adapter |
| `NOTICE` | Apache-2.0 attribution, copied into each edition |
| `tests/` | MCP framing, Onshape request signing, helpers, and every write guard against fake CAD sessions; `test_recovery.py` covers failed undo, restart, writes with no answer and two bridge processes; `test_solidcraft.py` the SolidCraft bridge's guards against a fake; `test_solidcraft_live.py` the SolidCraft bridge against real SolidCraft, on parts it makes in a temporary folder (skipped where SolidCraft is not installed, so in CI) |

## What is and isn't verified

- **Verified here:** the build, both plugins with `claude plugin validate` and the
  repository's own folder, frontmatter and licence checks, and the bridges'
  logic and guards against fake SOLIDWORKS, Onshape and SolidCraft sessions.
- **Run live, on test parts only:** the SolidCraft bridge, against SolidCraft 0.44.0's
  headless engine (commit a40f6d8) on Linux: variants on a copy with the base
  untouched, a set that would fail refused, restart and a changed copy refused, a part
  built from scratch (sketch, extrude, through cut, fillet, chamfer, Front-plane
  direction) and a STEP import. Not on anyone's own models, the desktop app,
  Windows or macOS.
- **Not verified:** the SOLIDWORKS and Onshape bridges against the real thing. SOLIDWORKS method names
  and enums were checked against a mirror of the SOLIDWORKS API Help; Onshape
  endpoints against Onshape's API documentation and its generated Go client.
  Neither bridge has talked to a live SOLIDWORKS session or Onshape account.
- **Built, unverified:** creating models from drawings or text (path C2/A: sketch,
  extrude, cut, fillet, chamfer), STEP import (C1), and listing bodies and external
  references for the integrity check. Each build step is checked and undone on
  failure; the SOLIDWORKS default part template enum (8) is not confirmed.
- **Recovery, unverified live:** an undo is believed only once every value it
  touched reads back as before; a failed rebuild stays failed across restarts;
  an Onshape write with no answer quarantines the copy with a record of the
  attempt until `onshape_reconcile_copy` reads it; and the state file is changed
  only as one locked read-change-write, so two bridge processes cannot undo each
  other's marks. Tested against fakes only. Until a live run covers failure,
  restart, two sessions and the original's preservation, use disposable models.
- **Not built:** CAM. Neither SOLIDWORKS CAM nor Onshape CAM Studio has a published
  API, and SolidCraft has no CAM, so all three editions use the feature path, which
  upstream already defines, and say so. Threads, lofts and sheet metal are hand-entered beside the model.

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

SolidCraft: install SolidCraft (or build `solidcraft-cli` from its repository), set
`SOLIDCRAFT_ALLOW_PATHS` to a scratch folder holding a throwaway `.sc` part, start Claude
with `--plugin-dir plugins/manufacturing-solidcraft`, and ask for `sc_status`, then
`sc_open_base` on that part, `sc_read_parameters`, and a two-variant sweep into the
same folder. Then `sc_new_part` and one sketch and extrude with `sc_add_feature`.

Recovery, on disposable models only: see the failure, restart, two-session and
original-preserved steps in the live test checklist. For Onshape, starting the
bridge with `ONSHAPE_BRIDGE_TEST_DROP_NEXT_WRITE_ANSWER=1` sends the next Part
Studio or Variable Studio write for real and throws its answer away, once, so a
lost answer can be tested without pulling the network cable. Never set it
outside a test.
