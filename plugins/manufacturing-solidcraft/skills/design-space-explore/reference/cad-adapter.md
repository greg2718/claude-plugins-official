# One backend, one interface

SolidCraft is the CAD backend this plugin reaches. Nothing downstream of this
file should have to know that: build to the adapter surface below, not to
SolidCraft's own tools directly, so the skills stay the same as in the Fusion and
SOLIDWORKS and Onshape editions of this plugin.

Three callers use this file:

- **`design-space-explore`** — variants of an existing model. It reads
  parameters, applies parameter sets, reads properties and creates variant
  files. **It never designs from scratch.**
- **`rfq-to-quote`, step 7** — the design step. Path C2 (a drawing) and path A
  (text specs) build an estimating model in a **new design** with the
  create-from-scratch tools below. Path B adapts a prior part's model with the
  same copy, apply and untouched-check mechanism `design-space-explore` uses —
  `adapt-prior-model.md`. Path C1 imports a customer's STEP file into a new
  design and reads it. All three have been run only on generated test parts.
- **`quote-build`, the CAM path** — **not available in this edition.** Hours
  come from the feature path. See *What this version does not do*.

---

## SolidCraft — open source, headless, through the bundled bridge

[SolidCraft](https://github.com/tangency-labs/solidcraft) is an open-source
parametric CAD application (MIT or Apache-2.0), with its own MCP server built
in. The plugin declares one connector, `solidcraft`, in `.mcp.json`. It is
`servers/solidcraft_bridge.py`, a small MCP server that runs on the engineer's
machine, starts `solidcraft-cli mcp --headless` itself, and adds the checks
this file asks for on top of SolidCraft's tools.

- **Headless: the bridge holds its own design.** The design lives in the
  SolidCraft process the bridge started, not in the SolidCraft window on the
  engineer's screen. Nothing the engineer has open is read or changed, and
  nothing has to be open. The bridge opens files itself: the base by its path,
  copies by theirs.
- **Files only in allowed folders.** SolidCraft refuses a path outside the
  folders it was started with. Set `SOLIDCRAFT_ALLOW_PATHS` to the folders that
  hold the models and where copies go. A refused path is a setup problem:
  **say so and stop**.
- **Needs SolidCraft and Python 3.10+.** Nothing else: the MCP layer on both
  sides is standard library. `solidcraft-cli` must be on the PATH, or
  `SOLIDCRAFT_CLI` set to its full path. If the Python launcher is not
  `python3`, set `SOLIDCRAFT_BRIDGE_PYTHON`.
- **The bridge has been run against SolidCraft 0.44.0's headless engine, on
  Linux, on test parts it made itself** (`tools/cad-variants/tests/test_solidcraft_live.py`
  in the fork that generates this plugin). It has not been run on a plant's own
  models, on Windows or macOS, or with another SolidCraft version. Every tool
  says so. Say so in every output that came from it until a run on a real model
  has been recorded here. `sc_status` reports the SolidCraft version; on any
  version other than 0.44.0, say that too.

| Bridge tool | Adapter operation | SolidCraft tool or command it uses |
|---|---|---|
| `sc_status`, `sc_read_active_document` | Read the open design | `version`; `inspect_design` (path, unsaved changes, timeline, bodies, parameters) |
| `sc_open_base` | Open a base model and refuse writes to it | SHA-256 of the file on disk, recorded first; then `open`. The bridge never saves to that path |
| `sc_read_parameters` | Read parameters, and which drive geometry | `parameters.list`: user parameters, sketch dimensions and feature inputs, each with its expression, value, `uses` and `used_by` |
| `sc_read_properties` | Read computed properties, and regeneration health | Every timeline feature's error and warning (`inspect_design`); then `measure` (volume, bounding box per body) |
| `sc_save_copy_and_open` | Save as a new file, keep working in it, and register it as the only kind of file the bridge writes | `save` with a new path (a save-as) |
| `sc_open_copy` | Reopen a bridge-made file after a restart | SHA-256 against the bridge's last save; then `open` |
| `sc_apply_parameters` | Check the whole set, try it, apply it, undo it if a feature fails | `check_parameters` (a dry run, undone by SolidCraft); `edit.checkpoint`; `parameters.change`; `edit.restore_checkpoint` |
| `sc_save_active` | Save the variant file | `save` to the copy's own path |
| `sc_check_base_untouched` | Confirm the base did not move | SHA-256 of the base file against the one recorded |
| `sc_new_part` | Create a new design for an estimating model, saved at once and registered. Checks it holds no feature and no body | `new_design`; `save` |
| `sc_add_feature` | One sketch, extrude, cut, fillet or chamfer, checked before and after, undone on failure | `batch` (`sketch.create`, `sketch.rectangle.two_point`, `sketch.circle.center`, `sketch.line`, `sketch.finish`); `solid.extrude`, `solid.fillet`, `solid.chamfer`; `body_topology`; `edit.checkpoint` and `edit.restore_checkpoint` |
| `sc_import_step` | Import a customer's STEP file as a new design, and register it | `open` of the STEP file; `save` to a new `.sc` path |
| `sc_list_contents` | The integrity check: timeline, sketches, bodies, components | `inspect_design`; `component.list` |
| `sc_read_faces` | Read bodies, faces and edges | `body_topology` (faces with area, centroid and plane normal; edges with ends, midpoint and length; cylinders with radius, axis and origin) |

**Units.** Millimetres and degrees, both ways. `sc_read_parameters` returns
values in millimetres (or degrees), and `sc_apply_parameters` takes an
expression as SolidCraft takes it (`"42 mm"`, `"30 deg"`, `"w / 2"`) or a
number in millimetres. Show the engineer's units beside them if they differ.

**Other ways in.** SolidCraft's own MCP server (`solidcraft-cli mcp`, and
`--connect` to drive the running desktop app) can be connected directly with
`custom-connector`. It is far wider than this adapter surface — it can save
over any file in the allowed folders — and none of the checks below would hold.
This edition uses it only through the bridge.

---

## What every backend implementation has to expose

### Variant operations — `design-space-explore`

| Operation | What it does |
|---|---|
| Read parameters | List the model's parameters and which drive geometry |
| Apply a parameter set | Regenerate the model against one set of values |
| Read computed properties | Volume, bounding box, body count |
| Create a variant document | A new file, never the base |

Everything in `parametric-check.md` and `variant-generation.md` is written
against this surface.

**A variant is always a new file.** SolidCraft keeps parameters, sketches and
features in the one `.sc` file; there are no configurations to add. Each
variant is a copy of the base, made with `sc_save_copy_and_open`.

### Open, apply, save as new — `design-space-explore` and `rfq-to-quote` path B

The sequence is `adapt-prior-model.md`. The calls it needs:

| Operation | What it does | Notes |
|---|---|---|
| Open and protect the base | Fingerprint the base file, open it, refuse writes to it | `sc_open_base` with the base's path. One call does both |
| Save as new file | Write a copy and keep working in it | `sc_save_copy_and_open`. **Before** any parameter change. Refused if the open design is not a base with no changes |
| Apply a parameter set | As above | `sc_apply_parameters`, with `expected_path` the copy. Refused anywhere else |
| Save the variant | Write the copy's file | `sc_save_active` |
| Read the base's fingerprint | Confirm it did not move | `sc_check_base_untouched`, after the run. A changed file stops the run |

For the next variant, open the base again with `sc_open_base`: a copy of a copy
is refused, so every variant starts from the base. **The base is never saved,
never edited in place, and never written to.** See *What the bridge enforces,
and what it cannot check*.

---

### Create-from-scratch operations — `rfq-to-quote` step 7, paths C2 and A

Always in a **new design the step creates itself** with `sc_new_part`, named
for the inquiry, line and revision (`<inquiry>_L<line>_<part>_R<rev>.sc`).
Never in a design that holds anything else, and never in a customer's file: the
bridge refuses to build in any file it did not make. Report the new design's
path.

| Operation | Bridge call | Notes |
|---|---|---|
| Create sketch | `sc_add_feature`, kind `sketch` | On the Front, Top or Right plane; rectangles, circles and lines in millimetres, each dimension from the drawing with its view and zone. The planes' axes are under *Behaviors that break a naive script* |
| Extrude | kind `extrude`, operation `NEW` or `ADD` | Stock and the main prismatic form; `BLIND` with a depth, or `THROUGH_ALL` |
| Hole / cut | kind `extrude`, operation `REMOVE` | A through hole is a circle cut `THROUGH_ALL` (both ways from the sketch plane); a pocket or blind bore is `BLIND`. A counterbore is two cuts, and a thread is recorded as a note, not modelled |
| Fillet | kind `fillet`: one point on each edge, and the radius | Each point must lie on exactly one edge, checked before the fillet; after it, the body must have gained a face |
| Chamfer | kind `chamfer`: one point on each edge, and the distance | Equal distance. A "break all edges" note is a chamfer set, cited to the note |
| Read back | `sc_read_properties`, `sc_list_contents` | Bodies, volume; checked against a hand calculation from the drawing |

Each step is unsaved until `sc_save_active`. Path A uses the same calls with
dimensions read from text. Anything else — threads, lofts, sheet metal — is not
built: hand-enter it beside the model and say so.

### STEP import — `rfq-to-quote` path C1

| Operation | Bridge call | Notes |
|---|---|---|
| Import STEP to a new design | `sc_import_step` with the STEP path and a new `.sc` path | The STEP file is read, never changed, and its SHA-256 recorded and checked after. It has to be in an allowed folder |
| Read bodies and faces | `sc_read_faces`, `sc_read_properties` | Read-only. A STEP file carries bodies, not a feature history: holes and pockets are recognised from cylindrical and planar faces, and each one is a reading to confirm at Gate 3 |

**Nothing is generated on path C1** — no sketch, no extrude, no change to the
imported bodies. Where the import fails, `rfq-to-quote` reads the STEP file as
text without CAD, labelled unverified, or takes a hand-entered feature list.

---

## What this version does not do

| Caller | Upstream behaviour | In this edition |
|---|---|---|
| `quote-build` CAM path | Setups, operations, toolpaths, toolpath points and machining time | **Not available.** SolidCraft has no CAM. Hours come from the feature path; the internal sheet says *"feature path only: no CAM connection in this edition."* Toolpath verification by geometry goes with it |
| Mass | Read from the CAD material | Not read. `sc_read_properties` gives volume; multiply by the plant's density for the material, and say so |
| Path C2 features beyond prismatic ones | Built if they work, said to be unverified | Not built: threads, lofts, sweeps, sheet metal. Hand-entered beside the model, and said so |

---

## Behaviors that break a naive script

Seen when the bridge was run against SolidCraft 0.44.0. **Read these before the
first call.**

- **SolidCraft applies a parameter value that breaks the model.**
  `parameters.change` keeps the new value and leaves the failing feature in the
  timeline. So `sc_apply_parameters` tries every set first with
  `check_parameters`, which SolidCraft undoes itself, and applies only a set
  that rebuilds. A set that fails is reported as failed and the copy is left as
  it was.
- **Setting an unknown name creates a parameter.** SolidCraft's
  `set_parameter` makes a new one. `sc_apply_parameters` refuses a name that is
  not already in the design.
- **Setting a computed value breaks its link.** A sketch dimension or feature
  input whose expression reads a user parameter (`d1 = h`) loses that link if
  it is set directly. Vary the parameter it reads. `sc_read_parameters` marks
  such rows `settable: false`; `sc_apply_parameters` refuses them.
- **Health is in the timeline.** A feature that fails keeps its place, with an
  error. `sc_read_properties` reads every feature's error and will not measure
  a design with one.
- **A save over an existing file keeps the old one as `.bak`.** The bridge
  saves a copy only to a new path, so the first save makes no `.bak`; each
  later `sc_save_active` leaves the previous save beside it.
- **The headless design dies with its process.** If SolidCraft stops, or the
  bridge restarts, the design in memory is gone, and so are changes not yet
  saved with `sc_save_active`. Reopen a copy with `sc_open_copy`: it checks the
  file is as the bridge last saved it, and retires it otherwise.
- **Linked components are not reported.** SolidCraft's component list does not
  say whether a component comes from another file. `sc_list_contents` returns
  `external_references_present: false` when the design holds no components,
  and `unknown` when it holds any.
- **Sketch planes and their axes.** Front: sketch x is model +X, sketch y is
  model +Z, and a positive extrude goes toward -Y. Top: x is +X, y is +Y, extrude
  toward +Z. Right: x is +Y, y is +Z, extrude toward +X. SolidCraft is Z-up.
- **Edges are named by a point on them.** A fillet or chamfer takes one point
  on each edge, in model coordinates (millimetres). On a straight edge, any
  point along it; on a curved edge, the midpoint `sc_read_faces` gives. Pick the
  point mid-edge: a point at a corner touches several edges and is refused.
  Tangent edges are not added: each edge is named.
- **One session, one caller.** The bridge runs one SolidCraft process and one
  design at a time. Do not run two batches at once.

---

## What the bridge enforces, and what it cannot check

These hold whatever order the calls come in, because the bridge enforces them
itself rather than relying on the steps being followed:

- **It writes only to a file it made.** `sc_apply_parameters`,
  `sc_add_feature` and `sc_save_active` refuse every file that
  `sc_save_copy_and_open`, `sc_new_part` or `sc_import_step` did not create.
  A base opened with `sc_open_base` is protected in the same call. Each write
  names the path it expects, and both the bridge and SolidCraft must report that
  path as the open design. A copy of a copy is refused.
- **It never overwrites a file.** A new copy, part or import goes only to a path
  that does not exist (nor its `.bak`).
- **It tries a whole parameter set before changing anything**: every name
  exists, is set directly and evaluates now. A set that would fail a feature is
  not applied. A set that is applied and still fails, or reads back other
  values, is undone with the checkpoint taken just before it, and the design is
  read back. If that does not bring back the timeline, bodies and parameters as
  they were, the copy is retired: discard it and make a new one.
- **Each build step is checked and undone on failure.** `sc_add_feature`
  checks the whole step before changing anything. Afterwards no feature may
  fail, a new extrude must make a body, a cut must remove material and an added
  extrude add it, a fillet or chamfer must change the body and add a face, and
  the timeline must be the old one plus exactly this feature. Otherwise the step
  is undone with its checkpoint, also when a check after the change stops with
  an unexpected error. If the undo does not read back, the file is retired.
- **It checks every bridge-made file before reopening it.** Each save records
  the file's SHA-256. `sc_open_copy` refuses, and retires, a file that no
  longer matches.
- **When SolidCraft stops answering, nothing is guessed.** The process is
  stopped and its design dropped. If it stopped during a save, that copy is
  retired.
- **Its state fails closed.** The list of protected bases and bridge-made files
  is written atomically under a lock file. Every change to it is one step under
  that lock: read the file again, change it, write it. A copy another bridge
  process retired stays retired. If the file cannot be read, the bridge writes
  nothing until a person checks it and fixes or removes it
  (`~/.manufacturing-solidcraft/protected.json` unless
  `SOLIDCRAFT_BRIDGE_STATE` says otherwise).

What it cannot check: whether a file it never opened was changed by someone
else while the bridge was not looking, beyond the SHA-256 checks above; and
anything the engineer does in the SolidCraft desktop app, which the bridge does
not reach.

## Document integrity — before and after every design step

The bridge does not replay scripts the way the Fusion add-in can, but the
check stays, because a write landing in the wrong document is the failure that
costs most.

1. **Before**, read-only: `sc_list_contents`. The open path is the expected
   file's. A new design lists no feature and no body; a copy lists the base's
   features, feature for feature.
2. Run the step. `sc_add_feature` runs this check itself for each feature and
   returns it as `document_integrity`.
3. **After**, read-only: `sc_list_contents` again. The features are the ones
   before plus only what this step created, by name. `sc_check_base_untouched`
   on any base.
4. **Anything else is unexpected content and the step fails.** Record the
   result in the geometry estimate's `document_integrity`, or the variant's
   `status_detail`, with both lists.

The result is `clean` only when both reads returned features and bodies and
they match. If SolidCraft returned no feature or body list, it is
`not_checked`, with the reason. "Not checked" is a result, and it is reported
as one.

---

## Record which backend ran

`cad_backend` travels with every output that came from CAD — `solidcraft`, or
`none` for anything hand-authored — on the variant manifest and on the
geometry estimate alike. Record the SolidCraft version and commit
(`sc_status`) in the manifest's `source_ref`, because a model rebuilt in
another version can measure differently.
