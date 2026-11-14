# Open a prior model, apply parameters, save as new

**One mechanism, two callers.** Both start from a model that already exists,
change some of its driving parameters, and keep the result in a new document.
Neither ever writes to the model they started from.

- **`design-space-explore`**, step 4 — once per variant, from the engineer's
  base model. The variant plan says which parameters and which values.
- **`rfq-to-quote`, step 7, path B** — once per line, from a *different* prior
  part's model that the customer or the estimator named as the starting point.
  The changed parameters come from the customer's drawing.

Everything here runs through the adapter surface in `cad-adapter.md`, and
every behavior listed there applies. Every write names the document it expects
to act on, and the bridge refuses a protected base.

---

## The rule this exists to keep

**The prior model is never overwritten.** Not saved over, not edited in place
and saved, not "updated to the new revision." It is the thing the engineer, or
the next quote, starts from, and an accidental edit to it is not recoverable by
re-running anything: the input the next run needs would already be gone.

A request to edit the prior model in place, *"it's quicker, just resave it"*,
is declined in one line, and the new-document route is offered instead. It
takes one extra call.

---

## The sequence

### 1. Open the prior model, and read it without touching it

**The user gives the prior model's Part Studio URL**; the document,
workspace and element ids are in it. Then protect it (`onshape_protect_base`).

Read-only, record:

- its name, its document reference and its version number
- its parameters, and which of them drive geometry (`parametric-check.md`)
- its feature list, by name and type (`onshape_read_parameters`; sketches are features).
  Its bodies cannot be listed through the bridge yet: record them as unknown
- **whether it has unsaved changes.** If it does, stop. Someone has work open
  in it, and a save-as would carry that work into the new document. Ask the
  user to save or discard it themselves.

### 2. Save as a new document, before any parameter changes

Save the open model under the new name first — the caller's naming
convention, below — and **confirm, read-only, that the active document is now
the new one**, by name, before the first write. From here on every write lands
in the new document. If the active document is still the prior model, stop.

| Caller | New document name |
|---|---|
| `design-space-explore` | The variant's ID, traceable to its parameter set (`variant-generation.md`) |
| `rfq-to-quote` path B | `<inquiry>_L<line>_<part>_R<rev>`, the same as a model built from a drawing |

`design-space-explore` may instead use a clearly-named configuration of the
base where the engineer asked for one. Path B always uses a new document.

### 3. Integrity check, before

Read the new document's feature list (`onshape_read_parameters`), read-only. **It must match
the prior model's list from step 1, feature for feature.** Anything else is
unexpected content: the copy is not the one expected, or something else wrote
to it. Stop, and tell the user what was found.

**The bridge cannot list bodies or external references yet.** So this check is
never recorded as `clean`: record `document_integrity.result: not_checked`,
with both feature lists, and `detail` saying the feature lists matched but
bodies and external references could not be read by the Onshape bridge.

### 4. Apply the parameters, and verify the regeneration

Apply the whole parameter set in one call: the bridge checks every change
before it writes any, and undoes the set if one fails part way. Then confirm
the model regenerated with no failed feature. **Only `regenerated: true` is a
pass**: false is a failure, and a missing or unknown health is treated as one. A failure is reported with the feature that failed and
why: for `design-space-explore` the row is `regeneration_failed` and the batch
goes on; for path B the line falls back to C2 (below).

### 5. Read back

Bodies, bounding box, volume, and whatever else the caller records. For path
B, check each changed dimension against the drawing value it came from.

### 6. Integrity check, after

The same feature list, read again with `onshape_read_parameters`, plus only what this step
created. Anything else is unexpected content and the step fails, with both
lists recorded. As at step 3, the result is `not_checked`, never `clean`,
because bodies and external references are not read.

### 7. Confirm the prior model is untouched

Read-only: the prior model's microversion is the one recorded at step 1
(`onshape_check_base_untouched`).
**If it moved, stop and tell the user at once**, naming the model and both
values. Restoring an earlier version is the user's call, made in
Onshape, never this skill's.

### 8. Record

Per run: the prior model's reference and version, the new document's
reference, each parameter as *from → to*, the regeneration result, both
integrity lists, the integrity result (`not_checked`, with the reason),
and the untouched check. `design-space-explore` writes these
into the variant manifest (`base_model`, per-variant `document_ref`); path B
writes them into the geometry estimate (`base_model_ref`, `document_ref`,
`document_integrity`).

---

## Path B only: what the parameters may change

**The drawing is still the geometry.** The prior model supplies a shape that
is already modelled; every dimension and every tolerance on the geometry
estimate is read from the customer's drawing and cites its view and zone,
exactly as for a model built from the drawing. A tolerance is never carried
over from the prior part.

Path B fits a line only where **every difference between the prior part and
the drawing is a driving parameter of the prior model.** Compare the drawing
with the prior model's parameters first, and list each change as *parameter:
prior value → drawing value, drawing view and zone*. Where a difference is a
feature the prior model does not have, or one it has and the drawing does not,
or a dimension no driving parameter controls, path B does not fit that line.

**When path B does not fit, the line falls back to C2**, which in this edition
is the hand-entered feature list, prefilled from the drawing as read (the Onshape
bridge does not build models from drawings yet; `cad-adapter.md`, *What this
version does not do*). Say it in one line — *"Path B: <prior model> <reason>;
hand-entered feature list from the drawing (C2), not built in CAD."* Never say
the line was built from the drawing: nothing was built. The reasons:

- the prior model is not on file, cannot be found, or the user cannot open it
- it has unsaved changes the user has not resolved
- a change on the drawing is not a driving parameter of the prior model
- the regeneration failed
- the read-back does not match the drawing

The geometry estimate is then emitted as `geometry_path: C2`, `origin:
hand_entered`, `cad_backend: none`, `document_ref: null`, `base_model_ref: null`,
every row unconfirmed until the estimator confirms it at Gate 3, with the
fallback and its reason as the first entry in `questions_for_estimator` so it is
seen at Gate 3.

**No Onshape connection at all** is not a path B fallback; it is the same degradation as
every path: the hand-entered feature list from the drawing, or no design step.

## What has and has not run

The variant mechanism above is `design-space-explore`'s, unchanged. **The
open, save-as-new and untouched-check calls have not been run through the
connector in this repo, for either caller.** Say so where a run depends on
them, and do not report the prior model as untouched without the step 7 read.
