# One backend, one interface

SOLIDWORKS is the CAD backend this plugin reaches. Nothing downstream of this
file should have to know that: build to the adapter surface below, not to the
SOLIDWORKS API directly, so the skills stay the same as in the Fusion and
Onshape editions of this plugin.

Three callers use this file:

- **`design-space-explore`** — variants of an existing model. It reads
  parameters, applies parameter sets, reads properties and creates variant
  files. **It never designs from scratch.**
- **`rfq-to-quote`, step 7** — the design step. Path B adapts a prior part's
  model with the same copy, apply and untouched-check mechanism
  `design-space-explore` uses — `adapt-prior-model.md`. Paths C2 and A (build
  from scratch) and C1 (STEP import) are **not implemented by the bridge in this
  version**: they take the hand-entered feature list, as when no CAD is
  connected. See *What this version does not do*.
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
  API Help; its behaviour was not. Every tool says so. Say so in every output
  that came from it until a run on a real model has been recorded here.

| Bridge tool | Adapter operation | SOLIDWORKS API it uses |
|---|---|---|
| `sw_status`, `sw_read_active_document` | Read the open document | `ISldWorks::ActiveDoc`, `RevisionNumber`; `IModelDoc2::GetTitle`, `GetPathName`, `GetType`, `GetSaveFlag`, `GetConfigurationNames`, `GetActiveConfiguration`; `IPartDoc::GetMaterialPropertyName2` |
| `sw_protect_base` | Record the base and refuse writes to it | SHA-256 of the saved file, on disk. `GetSaveFlag` must be false |
| `sw_read_parameters` | Read parameters, and which drive geometry | Feature tree (`FirstFeature`, `GetNextFeature`, `GetFirstSubFeature`), `IFeature::GetFirstDisplayDimension`, `IDisplayDimension::GetDimension2`, `IDimension::FullName`, `SystemValue`, `DrivenState`, `ReadOnly`; `IEquationMgr::GetCount`, `Equation`, `Value`, `GlobalVariable` |
| `sw_read_properties` | Read computed properties, and regeneration health | `IFeature::GetErrorCode2` on every feature; `IModelDocExtension::CreateMassProperty2` (`Mass`, `Volume`, `Density`); `IPartDoc::GetPartBox` |
| `sw_save_copy_and_open` | Save as a new file, make it active | `IModelDocExtension::SaveAs3` with `swSaveAsOptions_Copy`; `ISldWorks::OpenDoc6`; `ISldWorks::ActivateDoc3` |
| `sw_apply_parameters` | Apply a parameter set, regenerate | `IModelDoc2::Parameter` → `IDimension::SetSystemValue3` (this configuration); `IEquationMgr::Equation` (set); `IModelDoc2::ForceRebuild3` |
| `sw_save_active` | Save the variant file | `IModelDoc2::Save3` |
| `sw_check_base_untouched` | Confirm the base did not move | SHA-256 of the base file against the one recorded |

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
document during a write.** The bridge enforces the last two: a write names the
path it expects to be active, and a protected path is refused.

**PDM.** If the base lives in a SOLIDWORKS PDM vault, the copy goes to a working
folder **outside** the vault unless the engineer names a vault folder. Nothing
here checks a file in or out, changes a state, or adds a file to the vault.

---

## What this version does not do

Said plainly, so nobody finds out in a demo:

| Caller | Upstream behaviour | In this edition |
|---|---|---|
| `rfq-to-quote` path C2 (build from the drawing) and path A (from text) | Builds an estimating model feature by feature | **Not implemented.** The bridge has no create-from-scratch tools yet. Step 7 offers the hand-entered feature list, prefilled from the drawing as read, exactly as when no CAD is connected, and says why: *"SOLIDWORKS connected; the bridge does not build models from drawings yet. Hand-entered feature list."* |
| `rfq-to-quote` path C1 (STEP import) | Imports into CAD and reads the bodies | **Not implemented.** The STEP is read as text without CAD (unverified), else a hand-entered list. An engineer can still import it in SOLIDWORKS themselves and save it; reading that saved file with `sw_read_properties` is a read, and is labelled as reading a model the engineer imported |
| `quote-build` CAM path | Toolpaths and machining time | **Not available.** SOLIDWORKS CAM is not driven from here. Hours come from the feature path; the internal sheet says *"feature path only: no CAM connection in this edition."* |

The SOLIDWORKS API can do all three (feature creation through
`IFeatureManager` and `ISketchManager`, STEP import through
`ISldWorks::LoadFile4`); adding them to the bridge is the next piece of work,
and each one needs the same before-and-after integrity check as below.

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
  non-warning error is a failed variant, recorded as such, never measured.
- **Dialogs block automation.** A modal dialog in SOLIDWORKS (a rebuild error
  prompt, a missing reference) stops every COM call until a person dismisses
  it. If calls start timing out, ask the user to look at the SOLIDWORKS window.
- **External references.** A part with in-context references to an assembly
  can change when the assembly does, and a copy keeps pointing at the
  original's references. Detect them (`sw_read_parameters` shows the features)
  and warn before generating.
- **One SOLIDWORKS session, one caller.** COM calls into SOLIDWORKS run one at
  a time. Do not run two batches at once.
- **SOLIDWORKS must be open with the document active.** There is no file-only
  path in this bridge.

---

## Document integrity — before and after every design step

The bridge does not replay scripts the way the Fusion add-in can, but the
check stays, because a write landing in the wrong document is the failure that
costs most.

1. **Before**, read-only: `sw_read_active_document` and `sw_read_parameters`.
   The active path is the copy's, and the feature list is the base's.
2. Run the step.
3. **After**, read-only: the active path is still the copy's; every new feature
   is one this step created. `sw_check_base_untouched` on the base.
4. **Anything else fails the step.** Record the result in the geometry
   estimate's `document_integrity`, or the variant's `status_detail`, with both
   lists.

A clean result is recorded too. "Not checked" is a result, and it is reported
as one.

---

## Record which backend ran

`cad_backend` travels with every output that came from CAD — `solidworks`, or
`none` for anything hand-authored — on the variant manifest and on the
geometry estimate alike. Record the SOLIDWORKS revision (`sw_status`) in the
manifest's `source_ref`, because a model rebuilt in another release can
measure differently.
