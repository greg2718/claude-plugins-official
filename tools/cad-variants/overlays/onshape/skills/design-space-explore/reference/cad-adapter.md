# One backend, one interface

Onshape is the CAD backend this plugin reaches. Nothing downstream of this
file should have to know that: build to the adapter surface below, not to the
Onshape API directly, so the skills stay the same as in the Fusion and
SOLIDWORKS editions of this plugin.

Three callers use this file:

- **`design-space-explore`** — variants of an existing model. It reads
  parameters, applies parameter sets, reads properties and creates variant
  documents. **It never designs from scratch.**
- **`rfq-to-quote`, step 7** — the design step. Path B adapts a prior part's
  model with the same copy, apply and untouched-check mechanism
  `design-space-explore` uses — `adapt-prior-model.md`. Paths C2 and A (build
  from scratch) and C1 (STEP import) are **not implemented by the bridge in this
  version**: they take the hand-entered feature list, as when no CAD is
  connected. See *What this version does not do*.
- **`quote-build`, the CAM path** — **not available in this edition.** Hours
  come from the feature path.

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
  API client; its behaviour was not checked. Every tool says so. Say so in every
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

## What this version does not do

| Caller | Upstream behaviour | In this edition |
|---|---|---|
| `rfq-to-quote` path C2 (build from the drawing) and path A (from text) | Builds an estimating model feature by feature | **Not implemented.** Onshape's feature API takes a full feature definition per sketch and extrude; the bridge has no create tools yet. Step 7 offers the hand-entered feature list, prefilled from the drawing as read, and says why: *"Onshape connected; the bridge does not build models from drawings yet. Hand-entered feature list."* |
| `rfq-to-quote` path C1 (STEP import) | Imports into CAD and reads the bodies | **Not implemented.** The STEP is read as text without CAD (unverified), else a hand-entered list. If the engineer imports it into Onshape themselves, reading that Part Studio with `onshape_read_properties` is a read of a model the engineer imported, and labelled so |
| `quote-build` CAM path | Toolpaths and machining time | **Not available.** No CAM API was found in Onshape's public API documentation. Hours come from the feature path; the internal sheet says *"feature path only: no CAM connection in this edition."* |

The REST API documents what the first two need (`POST /documents`, feature
creation on a Part Studio, `POST /translations/d/{did}/w/{wid}` for imports);
adding them to the bridge is the next piece of work.

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

---

## What the bridge enforces, and what it cannot check

These hold whatever order the calls come in, because the bridge enforces them
itself rather than relying on the steps being followed:

- **It writes only to a copy it made.** `onshape_set_variables` and
  `onshape_create_version` refuse every document that `onshape_copy_workspace`
  did not create, and every workspace in it but the one the copy made,
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
- **It cannot list bodies or external references yet.** It lists features
  (sketches are features of type `newSketch`). So the integrity check compares
  feature lists and is recorded as `not_checked`, never `clean`, and
  `external_references_present` is `unknown`, never `false`.

## Document integrity — before and after every design step

1. **Before**, read-only: the copy's document name is the one expected, and
   its feature list (`onshape_read_parameters`, `features`) matches the base's,
   feature for feature.
2. Run the step.
3. **After**, read-only: the copy's feature list is the base's plus only what
   this step changed; `onshape_check_base_untouched` on the base.
4. **Anything else fails the step.** Record the result in the geometry
   estimate's `document_integrity`, or the variant's `status_detail`, with both
   lists.

**Matching feature lists are recorded as `not_checked`, not `clean`**, with the
reason: the bridge cannot list bodies or external references yet. "Not
checked" is a result, and it is reported as one.

---

## Record which backend ran

`cad_backend` travels with every output that came from CAD — `onshape`, or
`none` for anything hand-authored — on the variant manifest and on the
geometry estimate alike. `document_ref` is the Onshape URL of the element at
its version or microversion (`https://cad.onshape.com/documents/{did}/v/{vid}/e/{eid}`),
plus the encoded configuration where there is one, so the exact variant can be
reopened.
