# One backend, one interface

Onshape is the CAD backend this plugin reaches. Nothing downstream of this
file should have to know that: build to the adapter surface below, not to the
Onshape API directly, so the skills stay the same as in the Fusion and
SOLIDWORKS editions of this plugin.

Three callers use this file:

- **`design-space-explore`** — variants of an existing model. It reads
  parameters, applies parameter sets, reads properties and creates variant
  documents. **It never designs from scratch.**
- **`rfq-to-quote`, step 7** — the design step. Path C2 (a drawing) and path A
  (text specs) build an estimating model in a **new document** with the
  create-from-scratch tools below. Path B adapts a prior part's model with the
  same copy, apply and untouched-check mechanism `design-space-explore` uses —
  `adapt-prior-model.md`. Path C1 imports a customer's STEP file into a new
  document and reads it. All three are **unverified** on a live Onshape account.
- **`quote-build`, the CAM path** — **not available in this edition.** Hours
  come from the feature path. See *What this version does not do*.

---

## Onshape — cloud, through the bundled bridge

The plugin declares one connector, `onshape`, in `.mcp.json`. It is
`servers/onshape_bridge.py`, a small MCP server that runs on the engineer's
machine and calls Onshape's REST API with the engineer's API keys. Standard
library only: Python 3.10+ is all it needs.

- **Server-side, unlike Fusion.** Nothing has to be open in a browser. The
  bridge reaches any document the key's user can reach, by document id
  (`d/{did}`), workspace, version or microversion (`w`, `v`, `m`) and element
  (`e/{eid}`, a tab) — the URL of the tab in Onshape holds all three.
- **Credentials from the environment only.** `ONSHAPE_ACCESS_KEY` and
  `ONSHAPE_SECRET_KEY`, created in Onshape under *My Account → Developer* (or
  *Company Settings → Developer*), set where Claude starts. **Never asked for or
  pasted in the conversation** (`../../../shared/secret-config.md`). An
  Enterprise stack sets `ONSHAPE_BASE_URL` to `https://<company>.onshape.com`.
  Requests are signed with HMAC-SHA256 as Onshape's API-key documentation
  describes; a key gives the bridge exactly the access of the user who made it.
- **A read-only key is a good start.** Every read works with it, and Onshape
  answers 403 to a write — the bridge reports that plainly.
- **The bridge has not been run against a live Onshape account.** Its
  endpoints come from Onshape's API documentation
  (onshape-public.github.io/docs) and the endpoint list of Onshape's generated
  API client; its behaviour was not checked. The documentation shows a sketch
  with a circle, and an extrude; the line geometry, the fillet and chamfer
  parameters, and an import that makes a Part Studio from a STEP file are built
  from Onshape's feature schema and have no worked example to check against. Every tool says so. Say so in every
  output that came from it until a run on a real document has been recorded here.

| Bridge tool | Adapter operation | Onshape endpoint |
|---|---|---|
| `onshape_status` | Check the connection | `GET /users/sessioninfo` |
| `onshape_read_document` | Read the document | `GET /documents/{did}`, `/documents/d/{did}/workspaces`, `/documents/d/{did}/w/{wid}/elements` |
| `onshape_protect_base` | Record the base and refuse writes to it. Refuses when Onshape returns no microversion | `GET /documents/d/{did}/w/{wid}/currentmicroversion` |
| `onshape_read_parameters` | Read parameters, and which drive geometry | `GET /partstudios/d/{did}/{wvm}/{wvmid}/e/{eid}/features`; `GET /variables/.../variables`; `GET /elements/.../configuration` |
| `onshape_read_properties` | Read computed properties, and regeneration health | `features` (`featureStates`), `massproperties`, `boundingboxes`, each with an optional `configuration` |
| `onshape_encode_configuration` | Address one configuration | `POST /elements/d/{did}/e/{eid}/configurationencodings` |
| `onshape_eval_featurescript` | A measured property the REST reads do not give (a tight box). Pass the same `configuration` as the read it checks | `POST /partstudios/.../featurescript`, with the optional `configuration` |
| `onshape_copy_workspace` | Create a variant document, protect its source if needed, and register the copy as the only kind of document the bridge writes | `POST /documents/{did}/workspaces/{wid}/copy` |
| `onshape_set_variables` | Check the whole set, apply it, undo it if a write fails, read health after | Part Studio `#variable` features (`POST .../features/featureid/{fid}`, one per variable, then `GET .../features`) or a Variable Studio (`POST /variables/d/{did}/w/{wid}/e/{eid}/variables`, one call) |
| `onshape_create_version` | Pin a variant, in a bridge-made copy only | `POST /documents/d/{did}/versions` |
| `onshape_check_base_untouched` | Confirm the base did not move. Refuses to answer when either microversion is missing | the base workspace's current microversion against the one recorded |
| `onshape_create_document` | Create a new, private document for an estimating model, and register it as one the bridge writes. Checks its one Part Studio is empty | `POST /documents`; `GET /documents/d/{did}/w/{wid}/elements` |
| `onshape_add_feature` | One sketch, extrude, cut, fillet or chamfer, checked before and after, deleted again on failure | `POST /partstudios/d/{did}/w/{wid}/e/{eid}/features` (`BTMSketch-151`, `BTMFeature-134` for `extrude`, `fillet`, `chamfer`); `DELETE .../features/featureid/{fid}`; `POST .../featurescript` to count edges and faces |
| `onshape_import_step`, `onshape_import_status` | Import a customer's STEP file into a new document, and register it | `POST /documents`; `POST /translations/d/{did}/w/{wid}` (multipart upload); `GET /translations/{tid}` |
| `onshape_list_contents` | The integrity check: features, sketches, bodies, external references | `GET .../features`; `GET .../bodydetails`; derived features and reference parameters in the feature list |
| `onshape_read_faces` | Read bodies and faces of a Part Studio | `GET .../bodydetails` |

**The vendor's MCP server is a different tool.** PTC's *FeatureScript MCP
Server* (Onshape Labs, an early-access programme, announced August 2026, at
`https://fs-mcp.labs.onshape.app/mcp`) lets an AI client write, insert and test
FeatureScript custom features. Its published description covers writing
FeatureScript; it does not describe reading Part Studio variables or mass
properties, so it does not implement this adapter surface. A team that wants
Claude to author custom features adds it through `custom-connector`; its
calls count against the same API limit.

---

## Two ways to vary a model — prefer the one that writes nothing

**1. Configuration inputs: read-only variants.** Where the parameters to vary
are configuration inputs of the Part Studio, every variant is a *read* of the
base with a different `configuration`: encode the values
(`onshape_encode_configuration`), then `onshape_read_properties` with that
configuration. Nothing is copied, nothing is written, and the base cannot be
touched. The variant's `document_ref` is the base's reference plus the encoded
configuration. **Use this whenever the parameters allow it.**

**2. Variables: a copied document per variant.** Where the parameters are
`#variable` features or Variable Studio variables, a variant needs a write, and
a write never goes to the base:

1. `onshape_protect_base` on the base document. The bridge refuses every write
   to it from then on, across restarts. (Step 2 does this itself if it has not
   been done, but doing it first records the base before anything else runs.)
2. `onshape_copy_workspace` → a new document named for the variant (or, for
   path B, `<inquiry>_L<line>_<part>_R<rev>`).
3. `onshape_set_variables` on the copy, naming the copy in
   `expected_document_name`. Refused on any other document.
4. `onshape_read_properties` on the copy's workspace, measured only when
   `regenerated` is `true`.
5. Optionally `onshape_create_version` on the copy, so the result is pinned.
6. `onshape_check_base_untouched` at the end.

---

## What every backend implementation has to expose

### Variant operations — `design-space-explore`

| Operation | What it does |
|---|---|
| Read parameters | List the model's variables and configuration inputs, and which drive geometry |
| Apply a parameter set | Regenerate against one set of values — a configuration read, or a write to a copy |
| Read computed properties | Mass, volume, bounding box, whatever else the tool exposes |
| Create a variant document | A copied document, or a configuration of the base read without writing |

Everything in `parametric-check.md` and `variant-generation.md` is written
against this surface.

### Open, apply, save as new — `design-space-explore` and `rfq-to-quote` path B

The sequence is `adapt-prior-model.md`, with *open* meaning *read by id* and
*save as new* meaning *copy the workspace to a new document*.

| Operation | What it does | Notes |
|---|---|---|
| Read the document | Name, workspaces, tabs, current microversion | `onshape_read_document`. The user gives the URL of the prior model's Part Studio |
| Protect the base | Record its microversion, refuse writes | `onshape_protect_base` |
| Save as new document | Copy the workspace to a new document | `onshape_copy_workspace`. **Before** any parameter change |
| Apply a parameter set | As above | `onshape_set_variables` on the copy only |
| Read the prior model's microversion | Confirm it did not move | `onshape_check_base_untouched`. A moved microversion stops the run |

**Unsaved changes do not exist in Onshape** — every edit is saved as a
microversion as it is made. The equivalent risk is **someone editing the base
during the run**: the untouched check catches it, and the run stops.

---

### Create-from-scratch operations — `rfq-to-quote` step 7, paths C2 and A

Always in a **new document the step creates itself** with
`onshape_create_document`, named for the inquiry, line and revision
(`<inquiry>_L<line>_<part>_R<rev>`). Never in a document that holds anything
else, and never in a customer's: the bridge refuses to build in any document it
did not make. Report the new document's URL.

| Operation | Bridge call | Notes |
|---|---|---|
| Create sketch | `onshape_add_feature`, kind `sketch` | On the Top, Front or Right plane; rectangles, circles and lines in millimetres, each dimension from the drawing with its view and zone. Returns the sketch's feature id |
| Extrude | kind `extrude`, operation `NEW` or `ADD` | Stock and the main prismatic form; `BLIND` with a depth, or `THROUGH_ALL` |
| Hole / cut | kind `extrude`, operation `REMOVE` | A through hole is a circle cut `THROUGH_ALL`; a pocket or blind bore is `BLIND`. The Hole feature is not used: a counterbore is two cuts, and a thread is recorded as a note, not modelled |
| Fillet | kind `fillet`: one point on each edge, and the radius | Each point must lie on exactly one solid edge, checked by FeatureScript before the fillet; after it, the fillet must have made a face per edge |
| Chamfer | kind `chamfer`: one point on each edge, and the distance | Equal offsets. A "break all edges" note is a chamfer set, cited to the note |
| Read back | `onshape_read_properties`, `onshape_list_contents` | Bodies, volume; checked against a hand calculation from the drawing, or a tight box from `onshape_eval_featurescript` |

Path A uses the same calls with dimensions read from text. Anything else —
threads, lofts, sheet metal — is not built: hand-enter it beside the model and
say so. Each feature costs about seven API calls; a typical estimating model
of a dozen features costs about a hundred. Say so before building.

### STEP import — `rfq-to-quote` path C1

| Operation | Bridge call | Notes |
|---|---|---|
| Import STEP to a new document | `onshape_import_step` with the file path and the new document's name | The file has to be on the machine running the bridge; its SHA-256 is recorded. Onshape translates it; the bridge waits about two minutes, then `onshape_import_status` finishes it. An import that fails, or makes other than one new Part Studio, is not used |
| Read bodies and faces | `onshape_read_faces`, `onshape_read_properties` | Read-only. A STEP file carries bodies, not a feature history: holes and pockets are recognised from cylindrical and planar faces, and each one is a reading to confirm at Gate 3 |

**Nothing is generated on path C1** — no sketch, no extrude, no change to the
imported bodies. Where the import fails, `rfq-to-quote` reads the STEP file as
text without CAD, labelled unverified, or takes a hand-entered feature list.

---

## What this version does not do

| Caller | Upstream behaviour | In this edition |
|---|---|---|
| `quote-build` CAM path | Setups, operations, toolpaths, toolpath points and machining time | **Not available.** Onshape CAM Studio has no documented public API: it is not in Onshape's API documentation or API Explorer, and users on Onshape's forum are still asking for one. Hours come from the feature path; the internal sheet says *"feature path only: no CAM connection in this edition."* Toolpath verification by geometry goes with it |
| Path C2 features beyond prismatic ones | Built if they work, said to be unverified | Not built: threads, lofts, sweeps, sheet metal. Hand-entered beside the model, and said so |

---

## Behaviors that break a naive script

From Onshape's API documentation. **Read these before the first call.**

- **API calls are metered per year.** Onshape's published limits are 2,500
  calls per user per year on Free and Standard, 5,000 per user on Professional,
  and 10,000 per full user on Enterprise; past the limit every call answers
  402. A copied-document variant costs about eight calls (copy, name check,
  feature read, one write per variable, three reads); a configuration-read
  variant about four (encode, three reads). Only successful calls count. **Before a batch runs, show the call estimate beside the
  variant count**, and the plan's limit where the plant has said which plan it
  is on. A 3,000-variant sweep is past every plan's annual limit.
- **Rate limits are not published.** A 429 carries `Retry-After`; the bridge
  waits once, then reports. Do not poll faster than once a second.
- **Bounding boxes are approximate.** Onshape documents its bounding-box
  values as approximate, for graphics. Record them labelled so, and never use
  them for the read-back against a hand calculation; use volume, or a tight box
  from `onshape_eval_featurescript` with `evBox3d(context, { "topology" : qAllModifiableSolidBodies(), "tight" : true })`,
  passing the same `configuration` as the property read, or it measures the
  default configuration.
- **No material, no mass.** A body with no material reports `hasMass: false`.
  The bridge returns its mass as `null`, with the reason. Never score it.
- **Health is per feature.** `featureStatus` is `OK`, `INFO`, `WARNING`,
  `ERROR` or `UNKNOWN`. `ERROR` or `UNKNOWN` on any active feature is a failed
  variant, recorded as such, never measured. `WARNING` is reported and passes.
  A missing or unrecognised status is unknown health, and is not measured
  either.
- **Writes go to workspaces only.** Versions and microversions are immutable;
  a write names a `w`. Reading a version (`v`) is how a pinned variant is
  re-measured later.
- **A configured part has a different part id.** Do not carry part ids from
  one configuration to another; read them again.
- **References from other tabs and from FeatureScript are not seen.** The
  driving check reads expressions in the Part Studio's feature list. A
  variable used only inside custom FeatureScript, or by another Part Studio,
  shows as not driving this one. Say so when a variable shows no references.
  The same holds for external references: `onshape_list_contents` finds derived
  features and reference parameters in the feature list, and says what it
  scanned.
- **Build coordinates are in millimetres; reads are in metres.**
  `onshape_add_feature` takes millimetres, as a drawing gives them; sketch
  geometry goes to Onshape in metres and depths as expressions with units.
- **Edges are named by a point on them.** A fillet or chamfer takes one point on
  each edge, in Part Studio coordinates (millimetres). Pick the point mid-edge,
  away from corners: a point at a corner touches two edges and is refused.

---

## What the bridge enforces, and what it cannot check

These hold whatever order the calls come in, because the bridge enforces them
itself rather than relying on the steps being followed:

- **It writes only to a document it made.** `onshape_set_variables`,
  `onshape_add_feature` and `onshape_create_version` refuse every document that
  `onshape_copy_workspace`, `onshape_create_document` or `onshape_import_step`
  did not create, and every workspace in it but the one the bridge made,
  including a base that was never protected. Making a copy protects the source
  first if it is not protected yet. A copy of a copy is refused.
- **It checks every assignment before writing any.** If a write still fails
  part way, it undoes the earlier writes in that call and returns a change
  journal (from, to, written, undone). If the undo fails, it stops writing to
  that copy for good: discard it and make a new one.
- **Health must be positive evidence.** `regenerated` is `true` only when every
  active feature has a recognised state of `OK`, `INFO` or `WARNING`. A missing
  feature list, missing states or an unrecognised status is `null` (unknown),
  and `onshape_read_properties` will not measure it, exactly as for `ERROR`.
- **The untouched check needs real microversions.** Protecting a base refuses
  when Onshape returns no microversion, and `onshape_check_base_untouched`
  refuses to answer when either the recorded or the current microversion is
  missing, rather than comparing two empty values.
- **Its state fails closed.** The list of protected bases and bridge-made copies
  is written atomically under a lock file. If the file cannot be read, the
  bridge writes nothing until a person checks it and fixes or removes it
  (`~/.manufacturing-onshape/protected.json` unless `ONSHAPE_BRIDGE_STATE`
  says otherwise).
- **Each build step is checked and undone on failure.** `onshape_add_feature`
  checks the whole step before writing. Afterwards the feature must report `OK`,
  `INFO` or `WARNING`, the Part Studio must regenerate, a fillet or chamfer must
  have made a face per edge, and the feature list must be the old one plus
  exactly this feature. Otherwise it deletes the feature again and says so; if
  the delete fails, or features appear that this step did not make, it stops
  writing to that document for good.

## Document integrity — before and after every design step

1. **Before**, read-only: the document's name is the one expected, and
   `onshape_list_contents` on the Part Studio. A new document lists no feature
   and no body; a copy lists the base's features, feature for feature.
2. Run the step. `onshape_add_feature` runs this check itself for each feature
   and returns it as `document_integrity`.
3. **After**, read-only: `onshape_list_contents` again. The features are the
   ones before plus only what this step created, by feature id;
   `onshape_check_base_untouched` on any base.
4. **Anything else is unexpected content and the step fails.** Record the
   result in the geometry estimate's `document_integrity`, or the variant's
   `status_detail`, with both lists.

The result is `clean` only when both reads returned features and bodies and
they match. If Onshape returned no body list, it is `not_checked`, with the
reason. "Not checked" is a result, and it is reported as one.

---

## Record which backend ran

`cad_backend` travels with every output that came from CAD — `onshape`, or
`none` for anything hand-authored — on the variant manifest and on the
geometry estimate alike. `document_ref` is the Onshape URL of the element at
its version or microversion (`https://cad.onshape.com/documents/{did}/v/{vid}/e/{eid}`),
plus the encoded configuration where there is one, so the exact variant can be
reopened.
