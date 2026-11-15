# One backend, one interface

SOLIDWORKS is the CAD backend this plugin reaches. Nothing downstream of this
file should have to know that: build to the adapter surface below, not to the
SOLIDWORKS API directly, so the skills stay the same as in the Fusion and
Onshape editions of this plugin.

Three callers use this file:

- **`design-space-explore`** — variants of an existing model. It reads
  parameters, applies parameter sets, reads properties and creates variant
  files. **It never designs from scratch.**
- **`rfq-to-quote`, step 7** — the design step. Path C2 (a drawing) and path A
  (text specs) build an estimating model in a **new part** with the
  create-from-scratch tools below. Path B adapts a prior part's model with the
  same copy, apply and untouched-check mechanism `design-space-explore` uses —
  `adapt-prior-model.md`. Path C1 imports a customer's STEP file into a new
  part and reads it. All three are **unverified** on a live SOLIDWORKS session.
- **`quote-build`, the CAM path** — **not available in this edition.** Hours
  come from the feature path. See *What this version does not do*.

---

## SOLIDWORKS — desktop, live geometry, through the bundled bridge

The plugin declares one connector, `solidworks`, in `.mcp.json`. It is
`servers/solidworks_bridge.py`, a small MCP server that runs on the engineer's
own Windows machine and attaches to the SOLIDWORKS session already running
there through the SOLIDWORKS API (COM).

- **Windows only, SOLIDWORKS running, the document open and active.** The
  bridge attaches to a running session (`GetActiveObject("SldWorks.Application")`);
  it never starts SOLIDWORKS and never opens the base model by script. If no
  document is active, **say so and stop** — a precondition, not a degradation.
- **Needs Python 3.10+ and pywin32** on that machine. Nothing else: the MCP
  layer is standard library. If the Python launcher is not `python`, set
  `SOLIDWORKS_BRIDGE_PYTHON` (for example `py`).
- **The bridge has not been run against a live SOLIDWORKS session.** Its
  method names, signatures and enum values were checked against the SOLIDWORKS
  API Help (the feature-building calls against a markdown copy of it, because
  the Help site renders only in a browser); its behaviour was not. The one value
  not confirmed is the number for `swDefaultTemplatePart` (8); the template path
  it returns is checked before use, and `SOLIDWORKS_PART_TEMPLATE` overrides it. Every tool says so. Say so in every output
  that came from it until a run on a real model has been recorded here.

| Bridge tool | Adapter operation | SOLIDWORKS API it uses |
|---|---|---|
| `sw_status`, `sw_read_active_document` | Read the open document | `ISldWorks::ActiveDoc`, `RevisionNumber`; `IModelDoc2::GetTitle`, `GetPathName`, `GetType`, `GetSaveFlag`, `GetConfigurationNames`, `GetActiveConfiguration`; `IPartDoc::GetMaterialPropertyName2` |
| `sw_protect_base` | Record the base and refuse writes to it | SHA-256 of the saved file, on disk. `GetSaveFlag` must be false. `sw_save_copy_and_open` also does this when it has not been done |
| `sw_read_parameters` | Read parameters, and which drive geometry | Feature tree (`FirstFeature`, `GetNextFeature`, `GetFirstSubFeature`), `IFeature::GetFirstDisplayDimension`, `IDisplayDimension::GetDimension2`, `IDimension::FullName`, `SystemValue`, `DrivenState`, `ReadOnly`; `IEquationMgr::GetCount`, `Equation`, `Value`, `GlobalVariable`; the feature list (`IFeature::Name`, `GetTypeName2`) |
| `sw_read_properties` | Read computed properties, and regeneration health | `IFeature::GetErrorCode2` on every feature; `IModelDocExtension::CreateMassProperty2` (`Mass`, `Volume`, `Density`); `IPartDoc::GetPartBox` |
| `sw_save_copy_and_open` | Save as a new file, make it active, and register it as the only kind of file the bridge writes | `IModelDocExtension::SaveAs3` with `swSaveAsOptions_Copy`; `ISldWorks::OpenDoc6`; `ISldWorks::ActivateDoc3` |
| `sw_apply_parameters` | Check the whole set, apply it, undo it if a change fails, regenerate | `IModelDoc2::Parameter` → `IDimension::SetSystemValue3` (this configuration); `IEquationMgr::Equation` (set); `IModelDoc2::ForceRebuild3`, whose return value is checked |
| `sw_save_active` | Save the variant file | `IModelDoc2::Save3` |
| `sw_check_base_untouched` | Confirm the base did not move | SHA-256 of the base file against the one recorded |
| `sw_new_part` | Create a new part for an estimating model, and register it as a file the bridge writes. Checks it holds no sketch and no body | `ISldWorks::GetUserPreferenceStringValue` (default part template) or `SOLIDWORKS_PART_TEMPLATE`; `ISldWorks::NewDocument`; `IModelDocExtension::SaveAs3` |
| `sw_add_feature` | One sketch, extrude, cut, fillet or chamfer, checked before and after, deleted again on failure | `ISketchManager::InsertSketch`, `CreateCornerRectangle`, `CreateCircleByRadius`, `CreateLine`; `IFeatureManager::FeatureExtrusion3`, `FeatureCut4`, `FeatureFillet3`, `InsertFeatureChamfer`; `IModelDocExtension::SelectByID2`; `IEdge::GetClosestPointOn`; `IFeature::GetFaceCount`; `IModelDoc2::EditDelete` |
| `sw_import_step` | Import a customer's STEP file as a new part, and register it | `ISldWorks::LoadFile4`; `IModelDocExtension::SaveAs3` |
| `sw_list_contents` | The integrity check: features, sketches, bodies, external references | The feature list; `IPartDoc::GetBodies2`; `IModelDocExtension::ListExternalFileReferenceCount` |
| `sw_read_faces` | Read bodies and faces of a part | `IBody2::GetFaces`; `IFace2::GetArea`, `GetSurface`; `ISurface::IsPlane`, `PlaneParams`, `IsCylinder`, `CylinderParams` |

**Units.** The API works in SI: metres and radians. `sw_read_parameters`
returns SI values and `sw_apply_parameters` takes them. Convert from the
engineer's units yourself and show both. A global variable takes an expression
with its unit, as typed in SOLIDWORKS (`3.5mm`).

**Other ways in.** Several community MCP servers drive SOLIDWORKS through the
same COM API (MIT-licensed projects on GitHub such as `just1step/solidworks-mcp`
and `alisamsam/solidworks-mcp`, among others). None is published by Dassault
Systèmes, none exposes this adapter surface as it stands, and none has been
evaluated here. A plant that runs one can connect it through
`custom-connector`; this file's rules still apply to every call.

---

## What every backend implementation has to expose

### Variant operations — `design-space-explore`

| Operation | What it does |
|---|---|
| Read parameters | List the model's dimensions and global variables and which are driving geometry |
| Apply a parameter set | Regenerate the model against one set of values |
| Read computed properties | Mass, volume, bounding box, whatever else the tool exposes |
| Create a variant document | A new file, never the base |

Everything in `parametric-check.md` and `variant-generation.md` is written
against this surface.

**Configurations live inside the file.** A SOLIDWORKS configuration, or a
design table, is part of the base `.SLDPRT`: adding one *is* an edit to the
base. So in this edition a variant is always a **new file**. Where the engineer
asks for configurations, put them in **one copy** of the base — named for the
study — and never in the base itself.

### Open, apply, save as new — `design-space-explore` and `rfq-to-quote` path B

The sequence is `adapt-prior-model.md`. The calls it needs:

| Operation | What it does | Notes |
|---|---|---|
| Read the open document | Name, path, unsaved changes | `sw_read_active_document`. The user opens the prior model; the bridge does not open it |
| Protect the base | Fingerprint the base file, refuse writes to it | `sw_protect_base`. **Refuses a document with unsaved changes** |
| Save as new file | Write a copy, open it, make it active | `sw_save_copy_and_open`. **Before** any parameter change. It returns the active path; it must be the copy's |
| Apply a parameter set | As above | `sw_apply_parameters`, with `expected_path` the copy. Refused anywhere else |
| Read the base's fingerprint | Confirm it did not move | `sw_check_base_untouched`, after the run. A changed file stops the run |

**The prior model is never saved, never edited in place, and never the active
document during a write.** The bridge enforces this: a write names the path it
expects to be active, and only a copy the bridge made is written. See *What the
bridge enforces, and what it cannot check*.

**PDM.** If the base lives in a SOLIDWORKS PDM vault, the copy goes to a working
folder **outside** the vault unless the engineer names a vault folder. Nothing
here checks a file in or out, changes a state, or adds a file to the vault.

---

### Create-from-scratch operations — `rfq-to-quote` step 7, paths C2 and A

Always in a **new part the step creates itself** with `sw_new_part`, named for
the inquiry, line and revision (`<inquiry>_L<line>_<part>_R<rev>.SLDPRT`).
Never in a part that holds anything else, and never in a customer's file: the
bridge refuses to build in any file it did not make. Report the new part's path.

| Operation | Bridge call | Notes |
|---|---|---|
| Create sketch | `sw_add_feature`, kind `sketch` | On the Front, Top or Right plane; rectangles, circles and lines in millimetres, each dimension from the drawing with its view and zone. The plane must be named "Front Plane", "Top Plane" or "Right Plane"; a template that names them otherwise is refused, not guessed |
| Extrude | kind `extrude`, operation `NEW` or `ADD` | Stock and the main prismatic form; `BLIND` with a depth, or `THROUGH_ALL` |
| Hole / cut | kind `extrude`, operation `REMOVE` | A through hole is a circle cut `THROUGH_ALL`; a pocket or blind bore is `BLIND`. The Hole Wizard is not used: a counterbore is two cuts, and a thread is recorded as a note, not modelled |
| Fillet | kind `fillet`: one point on each edge, and the radius | Each point must lie on exactly one solid edge, checked before the fillet; after it, the fillet must own a face per edge |
| Chamfer | kind `chamfer`: one point on each edge, and the distance | Equal distance. A "break all edges" note is a chamfer set, cited to the note |
| Read back | `sw_read_properties`, `sw_list_contents` | Bodies, volume; checked against a hand calculation from the drawing. The bounding box is approximate: never the read-back |

Path A uses the same calls with dimensions read from text. Anything else —
threads, lofts, sheet metal — is not built: hand-enter it beside the model and
say so.

### STEP import — `rfq-to-quote` path C1

| Operation | Bridge call | Notes |
|---|---|---|
| Import STEP to a new part | `sw_import_step` with the STEP path and a new `.SLDPRT` path | The STEP file is read, never changed, and its SHA-256 recorded. A STEP that opens as an assembly is refused. The file has to be on the machine running SOLIDWORKS |
| Read bodies and faces | `sw_read_faces`, `sw_read_properties` | Read-only. A STEP file carries bodies, not a feature history: holes and pockets are recognised from cylindrical and planar faces, and each one is a reading to confirm at Gate 3 |

**Nothing is generated on path C1** — no sketch, no extrude, no change to the
imported bodies. Where the import fails, `rfq-to-quote` reads the STEP file as
text without CAD, labelled unverified, or takes a hand-entered feature list.

---

## What this version does not do

| Caller | Upstream behaviour | In this edition |
|---|---|---|
| `quote-build` CAM path | Setups, operations, toolpaths, toolpath points and machining time | **Not available.** SOLIDWORKS CAM's automation interface is not part of the published SOLIDWORKS API Help, so there is nothing documented to build the bridge against. Hours come from the feature path; the internal sheet says *"feature path only: no CAM connection in this edition."* Toolpath verification by geometry goes with it |
| Path C2 features beyond prismatic ones | Built if they work, said to be unverified | Not built: threads, lofts, sweeps, sheet metal. Hand-entered beside the model, and said so |

---

## Behaviors that break a naive script

From the SOLIDWORKS API Help, and from how COM automation of SOLIDWORKS is
known to behave. **Read these before the first call.**

- **The bounding box is approximate.** The API Help says the values from
  `IPartDoc::GetPartBox` and `IBody2::GetBodyBox` *"are approximate and should
  not be used for comparison or calculation purposes"* and *"may vary after
  rebuilding the model."* Record the box as a property, labelled approximate.
  **Never use it for the read-back against a hand calculation**; use volume.
- **No material, no mass.** A part with no material still returns a mass,
  computed from a default density (common SOLIDWORKS behaviour; not confirmed
  in the API Help pages checked). `sw_read_properties` reports the mass as
  unmeasured (`null`, with the reason) when no material is assigned. Never
  score that number.
- **A driven dimension cannot be set.** `SetSystemValue3` returns
  `swSetValue_DrivenDimension` (3) for a reference dimension. The parametric
  check reads `DrivenState` first; `sw_apply_parameters` refuses a driven one.
- **Equations win.** A dimension driven by an equation is overwritten by that
  equation at the next rebuild. Vary the **global variable** the equation
  reads, not the dimension. `sw_read_parameters` lists, per global variable,
  the dimensions it reaches.
- **Values change only in the active configuration.** The bridge sets
  dimensions with `swSetValue_InThisConfiguration`. A multi-configuration part
  is varied in the configuration that is active; say which.
- **A rebuild that "succeeds" can still fail features.** `ForceRebuild3`
  returning true is not health. Health is `GetErrorCode2` on every feature: any
  non-warning error is a failed variant, recorded as such, never measured. And
  `ForceRebuild3` returning false is a failed variant even with no feature
  error: the geometry may not have moved.
- **Dialogs block automation.** A modal dialog in SOLIDWORKS (a rebuild error
  prompt, a missing reference) stops every COM call until a person dismisses
  it. If calls start timing out, ask the user to look at the SOLIDWORKS window.
- **External references.** A part with in-context references to an assembly
  can change when the assembly does, and a copy keeps pointing at the
  original's references. `sw_list_contents` returns the count SOLIDWORKS
  reports: record `external_references_present` as `true` or `false` from it,
  and `unknown` when it returns none.
- **Build coordinates are in millimetres; reads are in metres.** `sw_add_feature`
  takes millimetres, as a drawing gives them, and converts; every read returns
  SI. Sketch x and y are the sketch plane's own axes.
- **Edges are named by a point on them.** A fillet or chamfer takes one point on
  each edge, in model coordinates (millimetres). Pick the point mid-edge, away
  from corners: a point at a corner touches two edges and is refused.
- **One SOLIDWORKS session, one caller.** COM calls into SOLIDWORKS run one at
  a time. Do not run two batches at once.
- **SOLIDWORKS must be open with the document active.** There is no file-only
  path in this bridge.

---

## What the bridge enforces, and what it cannot check

These hold whatever order the calls come in, because the bridge enforces them
itself rather than relying on the steps being followed:

- **It writes only to a file it made.** `sw_apply_parameters`,
  `sw_add_feature` and `sw_save_active` refuse every file that
  `sw_save_copy_and_open`, `sw_new_part` or `sw_import_step` did not create,
  including a base that was never protected. Making a copy protects the
  original first if it is not protected yet. A copy of a copy is refused, so
  every variant starts from the base.
- **It checks a whole parameter set before changing anything**: every
  dimension exists, is driving, is not read-only and is not set by an equation;
  every global variable exists; every value is a number. If a change still fails
  part way, it undoes the earlier changes in that call, then reads back every
  dimension and global variable the call touched. It reports the copy as it was
  only when each one reads its old value. An undo that raises, that SOLIDWORKS
  answers with a status other than success, or that does not read back stops
  writing to that copy for good: discard it and make a new one.
- **A failed rebuild is a failed variant.** If `ForceRebuild3` reports failure,
  the variant is `regeneration_failed` even when no feature shows an error,
  because the geometry may be the previous variant's. `sw_read_properties`
  will not measure it. The failure is kept in the state file, so it still
  holds after the bridge or SOLIDWORKS restarts; only a later rebuild that
  SOLIDWORKS reports as a success clears it. Health that cannot be read after a
  change counts the same way.
- **Its state fails closed.** The list of protected bases and bridge-made copies
  is written atomically under a lock file. Every change to it is one step under
  that lock: read the file again, change it, write it. A copy another bridge
  process retired stays retired, and every write checks the file again first,
  so two sessions on one machine cannot undo each other's safety marks. If the
  file cannot be read, the bridge writes nothing until a person checks it and
  fixes or removes it (`%USERPROFILE%\.manufacturing-solidworks\protected.json`
  unless `SOLIDWORKS_BRIDGE_STATE` says otherwise).
- **Each build step is checked and undone on failure.** `sw_add_feature`
  checks the whole step before changing anything. Afterwards the part must
  rebuild, a fillet or chamfer must own a face per edge, and the feature list
  must be the old one plus exactly this feature. Otherwise it deletes the
  feature again and says so, also when a check after the change stops with an
  unexpected error. If the delete fails, or features appear that this step did
  not make, it stops writing to that file for good.

## Document integrity — before and after every design step

The bridge does not replay scripts the way the Fusion add-in can, but the
check stays, because a write landing in the wrong document is the failure that
costs most.

1. **Before**, read-only: `sw_list_contents`. The active path is the expected
   file's. A new part lists no sketch and no body; a copy lists the base's
   features, feature for feature.
2. Run the step. `sw_add_feature` runs this check itself for each feature and
   returns it as `document_integrity`.
3. **After**, read-only: `sw_list_contents` again. The features are the ones
   before plus only what this step created, by name. `sw_check_base_untouched`
   on any base.
4. **Anything else is unexpected content and the step fails.** Record the
   result in the geometry estimate's `document_integrity`, or the variant's
   `status_detail`, with both lists.

The result is `clean` only when both reads returned features and bodies and
they match. If SOLIDWORKS returned no body list, it is `not_checked`, with the
reason. "Not checked" is a result, and it is reported as one.

---

## Record which backend ran

`cad_backend` travels with every output that came from CAD — `solidworks`, or
`none` for anything hand-authored — on the variant manifest and on the
geometry estimate alike. Record the SOLIDWORKS revision (`sw_status`) in the
manifest's `source_ref`, because a model rebuilt in another release can
measure differently.
