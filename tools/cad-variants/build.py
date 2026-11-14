#!/usr/bin/env python3
"""Build the SOLIDWORKS and Onshape editions of claude-for-manufacturing.

    python3 tools/cad-variants/build.py            # writes plugins/manufacturing-solidworks and plugins/manufacturing-onshape
    python3 tools/cad-variants/build.py --check    # fails if the committed editions differ from a fresh build

Each edition is the upstream plugin (plugins/claude-for-manufacturing), copied,
then changed in two ways:

1. Overlay files from tools/cad-variants/overlays/<edition>/ replace or add
   whole files: the CAD adapter, the README, the manifest, .mcp.json, the bridge.
2. Exact-text edits change every other place the upstream text names Autodesk
   Fusion. Each edit must find its text exactly once, so when upstream changes
   a sentence the build stops and says which, rather than silently shipping a
   half-converted plugin.

After both, the build fails if any file still mentions Fusion outside the
places allowed to (the README and NOTICE credit the upstream Fusion edition).
"""

from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
UPSTREAM = ROOT / "plugins" / "claude-for-manufacturing"
# Lines that may name Fusion: they credit the upstream edition or list it as another system.
ALLOWED_FUSION_LINES = {
    "README.md": [
        "with Autodesk Fusion replaced by",
        "only where they named Fusion",
        "edition**. Autodesk Fusion and",
        "| **Autodesk Fusion** | [`claude-for-manufacturing`]",
    ],
    "NOTICE": [""],
    "skills/design-space-explore/reference/cad-adapter.md": ["Fusion and", "unlike Fusion", "the Fusion add-in"],
    "skills/custom-connector/reference/known-systems.md": ["| Autodesk Fusion | 1 — vendor MCP, added via custom-connector |"],
}


@dataclass(frozen=True)
class Edition:
    key: str            # cad_backend value and overlay folder
    name: str           # how the CAD system is written
    plugin: str         # plugin folder and manifest name
    connect: str        # what has to be true before the design step can run
    open_prior: str     # how the prior model is reached
    doc: str            # "file" or "document"
    example_ref: str    # an example model reference
    example_variant_ref: str
    example_variant_ref_2: str
    example_estimate_ref: str
    example_prior_ref: str
    example_adapted_ref: str
    version_word: str   # what "version" means for the untouched check


SOLIDWORKS = Edition(
    key="solidworks",
    name="SOLIDWORKS",
    plugin="manufacturing-solidworks",
    connect="SOLIDWORKS running on the engineer's Windows machine with the model open and active, and the bundled bridge connected",
    open_prior="The user opens the prior model in SOLIDWORKS.",
    doc="file",
    example_ref="C:/Engineering/Fairview/BracketUpper.SLDPRT (sha256 3f9a…)",
    example_variant_ref="C:/Engineering/Fairview/Studies/BracketUpper-V001.SLDPRT",
    example_variant_ref_2="C:/Engineering/Fairview/Studies/BracketUpper-V002.SLDPRT",
    example_estimate_ref="C:/Estimates/EH-RFQ-0912_L1_PN-4471-B_R2.SLDPRT",
    example_prior_ref="C:/Engineering/Fairview/PN-4402-A.SLDPRT sha256 7c41… (fingerprint read again after the run: unchanged)",
    example_adapted_ref="C:/Estimates/EH-RFQ-0930_L1_PN-4402-C_R1.SLDPRT",
    version_word="file fingerprint (SHA-256)",
)

ONSHAPE = Edition(
    key="onshape",
    name="Onshape",
    plugin="manufacturing-onshape",
    connect="an Onshape API key set in the environment, the bundled bridge connected, and the URL of the base Part Studio",
    open_prior="The user gives the URL of the prior model's Part Studio.",
    doc="document",
    example_ref="https://cad.onshape.com/documents/<did>/v/<vid>/e/<eid> (Bracket, Upper, 4471 — version V7)",
    example_variant_ref="https://cad.onshape.com/documents/<did>/v/<vid>/e/<eid>?configuration=wall%3D3.0%2Bmm",
    example_variant_ref_2="https://cad.onshape.com/documents/<did>/v/<vid>/e/<eid>?configuration=wall%3D3.5%2Bmm",
    example_estimate_ref="https://cad.onshape.com/documents/<new did>/w/<wid>/e/<eid> (EH-RFQ-0912_L1_PN-4471-B_R2)",
    example_prior_ref="https://cad.onshape.com/documents/<did>/m/<mid>/e/<eid> PN-4402-A (microversion read again after the run: unchanged)",
    example_adapted_ref="https://cad.onshape.com/documents/<new did>/w/<wid>/e/<eid> (EH-RFQ-0930_L1_PN-4402-C_R1)",
    version_word="microversion",
)

EDITIONS = {e.key: e for e in (SOLIDWORKS, ONSHAPE)}


def tool(e: Edition, name: str) -> str:
    """The bridge's tool for an operation, in this edition's naming."""
    names = {
        "solidworks": {"new": "sw_new_part", "add": "sw_add_feature", "import": "sw_import_step",
                       "list": "sw_list_contents", "faces": "sw_read_faces", "read": "sw_read_parameters"},
        "onshape": {"new": "onshape_create_document", "add": "onshape_add_feature",
                    "import": "onshape_import_step", "list": "onshape_list_contents",
                    "faces": "onshape_read_faces", "read": "onshape_read_parameters"},
    }
    return names[e.key][name]


SW_SETUP_ROW = (
    '| **SOLIDWORKS** | On the Windows machine that runs SOLIDWORKS, install Python 3.10 or later and run `pip install pywin32`. The plugin starts its bridge itself, and it needs SOLIDWORKS open with the model. If the Python launcher is not `python`, set `SOLIDWORKS_BRIDGE_PYTHON` (for example `py`) |'
)
OS_SETUP_ROW = (
    '| **Onshape** | In Onshape: *My Account → Developer* (or *Company Settings → Developer* for a company account) → create an API key. Set `ONSHAPE_ACCESS_KEY` and `ONSHAPE_SECRET_KEY` in the environment Claude starts from, never in the conversation. On an Enterprise stack, also set `ONSHAPE_BASE_URL` to `https://<company>.onshape.com`. Needs Python 3.10 or later; if the launcher is not `python3`, set `ONSHAPE_BRIDGE_PYTHON` |'
)


def readme_edits(e: Edition) -> List[Tuple[str, str]]:
    N, k = e.name, e.key
    other = "Onshape" if k == "solidworks" else "SOLIDWORKS"
    if k == "solidworks":
        setup_row = SW_SETUP_ROW
        declared = ("The SOLIDWORKS bridge is declared in `.mcp.json` as a local program, because it attaches to\n"
                    "SOLIDWORKS on your own Windows machine through the SOLIDWORKS API.")
    else:
        setup_row = OS_SETUP_ROW
        declared = ("The Onshape bridge is declared in `.mcp.json` as a local program that calls Onshape's REST\n"
                    "API with your API keys; nothing has to be open in a browser.")
    return [
        ("# Claude for Manufacturing\n\n**Maintenance status:** Reference implementation. Not actively maintained or monitored, and not accepting contributions. Issues and pull requests may not receive a response. This project is provided AS IS, without warranty.",
         f"# Manufacturing — {N} edition\n\n**What this is:** a community edition of Anthropic's [Claude for Manufacturing](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/claude-for-manufacturing)\n"
         f"reference plugin, with Autodesk Fusion replaced by {N}. The skills are upstream's, changed\n"
         f"only where they named Fusion. The {N} bridge in `servers/` is new, and **has not yet been run\n"
         f"against a live {N} {'session' if k == 'solidworks' else 'account'}**: its calls were checked against {N}'s API documentation, not\n"
         f"exercised. Generated by `tools/cad-variants/build.py`; edit the overlays there, not this folder.\n"
         "Provided AS IS, without warranty, like the upstream plugin (see `NOTICE`)."),
        ("1. Install **Claude for Manufacturing** from the Claude plugin directory.",
         f"1. Install this folder as a plugin (`claude --plugin-dir plugins/{e.plugin}` from a checkout)."),
        ("needs a live CAD connection: **Autodesk Fusion on the desktop** in this plugin,\n**SOLIDWORKS** or **Onshape** through their editions of it (see\n[CAD editions](#cad-editions)), or a CAD system connected through\n`custom-connector`. A shop running Creo, NX or Inventor has no path to that one\nskill yet. Its partner",
         f"needs a live CAD connection: **{N}, through the bridge bundled with this\nedition**. Autodesk Fusion and {other} have editions of their own (see\n[CAD editions](#cad-editions)). Its partner"),
        ("Autodesk Fusion is declared in this plugin's `.mcp.json`, so its address is\nalready set. The SOLIDWORKS and Onshape [editions](#cad-editions) declare their\nown bridge in its place. Each needs one action from you before it will connect:",
         f"Only {N} is declared in this plugin's `.mcp.json`, as its bundled bridge — but\nit still needs one action from you before it will connect:"),
        ("| **Autodesk Fusion** | In the Fusion **desktop** app: *Preferences → General → API* → enable **Fusion MCP Server**. It runs locally on your machine and needs Fusion open with a document. If you changed the port from 27182, set `FUSION_MCP_PORT` |\n" + SW_SETUP_ROW + "\n" + OS_SETUP_ROW,
         setup_row),
        ("| `.claude-plugin/`, `.mcp.json` | The plugin manifest and the one declared connector (Fusion) |",
         f"| `.claude-plugin/`, `.mcp.json` | The plugin manifest and the one declared connector ({N}) |\n"
         f"| `servers/` | The {N} bridge: a standard-library MCP server the plugin starts itself |"),
        ("| Version | 1.0.0 |", "| Version | 1.0.0 (upstream), " + k + " edition 3 |"),
        ("| Connectors | 1 declared in `.mcp.json` — Autodesk Fusion.", f"| Connectors | 1 declared in `.mcp.json` — {N}, through the bundled bridge."),
        ("To run it from a local checkout: `claude --plugin-dir .`, then say \"set us up\".",
         "To run it from a local checkout: `claude --plugin-dir .` in this folder, then say \"set us up\"."),
        ("**When the RFQ is a drawing**, the chain builds the part in Fusion from the\ndrawing's dimensions, tolerances and notes, shows you the model, the tolerance\nreading and the operation list for a yes, then derives setup hours and cycle\ntime per operation — from the CAM toolpath where your cutting data is on file,\nand from the features otherwise.",
         f"**When the RFQ is a drawing**, the chain builds the part in {N} from the\ndrawing's dimensions, tolerances and notes, shows you the model, the tolerance\nreading and the operation list for a yes, then derives setup hours and cycle\ntime per operation from the features (this edition has no CAM connection)."),
        ("Without\nFusion it still runs, a rung down the ladder, and says so. A part close to one\nyou already make starts from that part's model, saved as a new document so the\noriginal is never touched; a 3D STEP file is read rather than rebuilt;",
         f"Without\n{N} it still runs, a rung down the ladder, and says so. A part close to one\nyou already make starts from that part's model in {N}, saved as a new {e.doc} so the\noriginal is never touched; a 3D STEP file is imported into a new {e.doc} and read rather than rebuilt;"),
        ("`design-space-explore` generates a batch of CAD variants directly in Fusion\nby applying changed parameters to the model you already have\nopen",
         f"`design-space-explore` generates a batch of CAD variants in {N}\nby applying changed parameters to the model you already have\n{'open' if k == 'solidworks' else 'in Onshape'}"),
        ("| RFQ to quote | customer-inquiry-intake → data-consolidate → design step in Fusion → quote-build, four gates |",
         f"| RFQ to quote | customer-inquiry-intake → data-consolidate → design step in {N} → quote-build, four gates |"),
        ("Oracle NetSuite or Infor M3 for the ERP records; Autodesk\nFusion for CAD and CAM, or SOLIDWORKS or Onshape for CAD in their\n[editions](#cad-editions).",
         f"Oracle NetSuite or Infor M3 for the ERP records; {N}\nfor CAD."),
        ("The same plugin comes in three editions, one per CAD system. The skills are the\nsame; only the CAD connection differs. Install the one your shop runs.\n\n| CAD system | Edition | How it connects |\n|---|---|---|\n| **Autodesk Fusion** | This plugin, `claude-for-manufacturing` | Fusion's own MCP server, in the desktop app |\n| **SOLIDWORKS** | [`manufacturing-solidworks`](../manufacturing-solidworks) | A bundled bridge to SOLIDWORKS on the engineer's Windows machine (Python and pywin32) |\n| **Onshape** | [`manufacturing-onshape`](../manufacturing-onshape) | A bundled bridge to Onshape's REST API, with the plant's API keys |\n\nAll three editions do the same CAD work",
         f"The same plugin comes in three editions, one per CAD system. The skills are the\nsame; only the CAD connection differs. This is the {N} edition.\n\n| CAD system | Edition | How it connects |\n|---|---|---|\n| **Autodesk Fusion** | [`claude-for-manufacturing`](../claude-for-manufacturing), Anthropic's original | Fusion's own MCP server, in the desktop app |\n| **SOLIDWORKS** | {'This plugin, ' if k == 'solidworks' else ''}[`manufacturing-solidworks`](../manufacturing-solidworks) | A bundled bridge to SOLIDWORKS on the engineer's Windows machine (Python and pywin32) |\n| **Onshape** | {'This plugin, ' if k == 'onshape' else ''}[`manufacturing-onshape`](../manufacturing-onshape) | A bundled bridge to Onshape's REST API, with the plant's API keys |\n\nAll three editions do the same CAD work"),
        ("(`shared/erp-connectors.md`). Fusion is declared in `.mcp.json` at a local address,\nbecause its MCP server runs inside the desktop app on your machine rather than\nin the cloud.",
         f"(`shared/erp-connectors.md`). {declared}"),
    ]


def edits(e: Edition) -> Dict[str, List[Tuple[str, str]]]:
    N, k = e.name, e.key
    return {
        # ------------------------------------------------------------------ design-space-explore
        "skills/design-space-explore/SKILL.md": [
            ("Generates a batch of CAD variants directly in Autodesk Fusion by applying",
             f"Generates a batch of CAD variants in {N} by applying"),
            ('compatibility: "Requires Autodesk Fusion (desktop, with the document open). There is no other CAD backend and no file-only path — without Fusion connected, the skill reports exactly what is needed rather than running degraded. Never overwrites the base model."',
             f'compatibility: "Requires {N} through the bundled bridge: {e.connect}. Without it the skill reports exactly what is needed rather than running degraded. Never overwrites the base model; every variant is a new {e.doc}."'),
            ("→ Connect: Fusion, with the base model open in the desktop app",
             f"→ Connect: {N} through the bridge — {e.connect}"),
            ("| The base model | Fusion (open desktop document) | Live CAD connection |",
             f"| The base model | {N}, through the bundled bridge | Live CAD connection |"),
            ("One implementation, Fusion, behind one interface. Read\n`reference/cad-adapter.md` for the interface and the connector behaviors that\nbreak a naive script.\n\n- **Fusion** — desktop only, and acts on the open document via command-based\n  operations on live geometry. There is no server-side fetch path; if the\n  document is not open on the user's machine, there is nothing to act on.",
             f"One implementation, {N}, behind one interface. Read\n`reference/cad-adapter.md` for the interface, the bridge's tools, and the\nbehaviors that break a naive script.\n\n- **{N}** — {e.connect}. If that precondition is not met, say exactly which\n  part is missing and stop.\n- **Protect the base first.** The bridge's protect call records the base's\n  {e.version_word} and refuses every write to it from then on."),
            ("- **Fusion isn't open, or the customer uses Fusion's web client** — the\n  connector cannot work at all. Detect and say so plainly; there is no\n  server-side fetch path.\n- **The connector replays an earlier script** — geometry nobody asked for\n  appears, or read-only calls fail with \"cannot modify.\" Stop and ask the user\n  to restart Fusion; see `reference/cad-adapter.md`.",
             f"- **The bridge is not connected, or its precondition is not met** — say\n  exactly what is missing ({e.connect}) and stop.\n- **The bridge refuses a write** — it names why: a protected base, or a\n  {e.doc} that is not the one expected. Never work around it; see\n  `reference/cad-adapter.md`."),
            ("**One CAD tool, on one platform.** The skill works for customers running\nFusion on the desktop, with no cloud path. There is no fallback CAD backend —\nno connector currently reaches SolidWorks, Creo, NX or Inventor, so a customer\non any of those has no path to this skill at all. State that plainly rather\nthan letting a prospect discover it for the first time in a demo. Keep the\nCAD-adapter layer regardless, so a CAD connector appearing later drops in\nwithout a redesign.\n\n**If Fusion is not available** — the skill cannot generate variants and says\nso, naming exactly what is needed: Fusion desktop with the document open.",
             f"**One CAD tool.** This edition works for customers running {N}, through\nthe bridge bundled with it. The bridge has not yet been run against a live\n{N} {'session' if k == 'solidworks' else 'account'}: say so on every manifest it produces until it has.\nState that plainly rather than letting a prospect discover it in a demo.\n\n**If {N} is not available** — the skill cannot generate variants and says\nso, naming exactly what is needed: {e.connect}."),
        ],
        "skills/design-space-explore/reference/parametric-check.md": [
            ("2. **Test whether changing it regenerates dependent geometry.** Where the\n   CAD tool supports it, this can be a small probe change and revert rather\n   than a destructive test.",
             f"2. **Test whether changing it regenerates dependent geometry.** First read\n   it: the bridge's parameter read says which variables reach geometry. Where a\n   probe is still needed, make it **in a copy, never in the base** — a\n   probe-and-revert on the base is still a write to the base{', and in Onshape it leaves a microversion behind' if k == 'onshape' else ''}."),
        ],
        "skills/design-space-explore/reference/adapt-prior-model.md": [
            ("every connector behavior listed there applies: `run(context)`, `print()` as the\nonly output, `readOnly: true` on every inspection call, each call under about\n25 seconds, the Design workspace active.",
             "every behavior listed there applies. Every write names the "
             + e.doc + " it expects\nto act on, and the bridge refuses a protected base."),
            ("Fusion has no file-only path: **the prior model must be open in the desktop\napp.** Ask the user to open it. Opening it from the hub by script is not\nverified on this connector; do not rely on it.",
             (f"**The prior model must be open and active in {N}.** Ask the user to open\nit; the bridge never opens it by script. Then protect it (`sw_protect_base`)."
              if k == "solidworks" else
              "**The user gives the prior model's Part Studio URL**; the document,\nworkspace and element ids are in it. Then protect it (`onshape_protect_base`).")),
            ("add-in is replaying an earlier script. Stop, and ask the user to restart\nFusion.",
             "copy is not the one expected, or something else wrote to it. Stop, and\ntell the user what was found."),
            ("Read-only: the prior model's version number is the one recorded at step 1.\n**If it moved, stop and tell the user at once**, naming the model and both\nversion numbers. Restoring an earlier version is the user's call, made in\nFusion, never this skill's.",
             f"Read-only: the prior model's {e.version_word} is the one recorded at step 1\n(`{'sw_check_base_untouched' if k == 'solidworks' else 'onshape_check_base_untouched'}`).\n**If it moved, stop and tell the user at once**, naming the model and both\nvalues. Restoring an earlier version is the user's call, made in\n{N}{' or PDM' if k == 'solidworks' else ''}, never this skill's."),
            ("**No Fusion at all** is not a path B fallback;",
             f"**No {N} connection at all** is not a path B fallback;"),
        ],
        "skills/design-space-explore/reference/variant-generation.md": [
            ("**A live CAD connection makes 3,000 low-value variants exactly as easily as\n30 good ones.**",
             "**A live CAD connection makes 3,000 low-value variants exactly as easily as\n30 good ones.**"
             + (" On Onshape it also spends the plant's annual API calls: show the\nestimated calls beside the count (`cad-adapter.md`, *Behaviors that break a\nnaive script*)." if k == "onshape" else "")),
        ],
        # ------------------------------------------------------------------ rfq-to-quote
        "skills/rfq-to-quote/SKILL.md": [
            ("builds an internal estimating model in Fusion where connected (from the drawing, a prior part's model saved as new, text specs as a ROM only, or a STEP file),",
             f"builds an internal estimating model in {N} where connected (from the drawing, a prior part's model saved as new, text specs as a ROM only, or a STEP file),"),
            ("and Autodesk Fusion (desktop, open) for the design step and CAM hours;",
             f"and {N} through the bundled bridge for the design step;"),
            ("→ Fusion missing? Hand-entered features or no design step. Labelled, never a stop",
             f"→ {N} missing? Hand-entered features or no design step. Labelled, never a stop"),
            ("| Fusion, for the design step | The desktop app, with the connector | Live CAD connection |",
             f"| {N}, for the design step | The bundled bridge | Live CAD connection |"),
            ("in a new Fusion document, every feature citing its source, with the",
             f"in a new {N} {e.doc}, every feature citing its source, with the"),
            ("the CAM path where Fusion\nManufacturing and plant cutting data exist, the feature path always",
             "the feature path (this edition has no CAM\nconnection)"),
            ("- **Foreign geometry appears in the Fusion document** — the step fails. Ask\n  the user to restart Fusion. Never build on top of it.",
             f"- **The integrity check finds something it did not expect in the {e.doc}** —\n  the step fails. Tell the user what was found. Never build on top of it."),
            ("| Fusion | **The chain still completes.**",
             f"| {N} | **The chain still completes.**"),
            ('Say "Fusion not connected — no model built" on the internal sheet |',
             f'Say "{N} not connected — no model built" on the internal sheet |'),
            ("| Fusion Manufacturing (CAM) | Feature path only, and the label says so |\n| Plant cutting data | The CAM path runs on library feeds, labelled `fusion_default`, rung 4 |",
             "| CAM | Not connected in this edition. Feature path only, and the label says so |\n| Plant cutting data | The feature path cannot time a cut; that op is rung 4 |"),
            ("| Foreign geometry in the document | Step 7 fails for that line. The user restarts Fusion |",
             f"| Unexpected content in the {e.doc} | Step 7 fails for that line. The user is told what was found |"),
            ("| `../design-space-explore/reference/cad-adapter.md` | Step 7, before the first Fusion call — the create-from-scratch, save-as-new and STEP import calls, and the connector behaviors that break them |",
             f"| `../design-space-explore/reference/cad-adapter.md` | Step 7, before the first {N} call — the bridge's create-from-scratch, save-as-new and STEP import tools, what this edition does not build, and the behaviors that break a naive script |"),
        ],
        "skills/rfq-to-quote/reference/design-step.md": [
            ("Read `../../design-space-explore/reference/cad-adapter.md` first: the\ncreate-from-scratch calls, and the connector behaviors that lose calls or plant\nforeign geometry. The rules that matter most:\n\n- Every script defines `run(context)` and prints what it needs back.\n- Inspection calls are `readOnly: true`.\n- Each call under about 25 seconds.\n- Design workspace active on every call.\n- Fusion open with the document active; there is no file-only path.\n\nThen:",
             f"Read `../../design-space-explore/reference/cad-adapter.md` first: the bridge's\ncreate-from-scratch tools, and the behaviors that break a naive build. The\nrules that matter most:\n\n- {N} connected: {e.connect.replace(', and the URL of the base Part Studio', '').replace(' with the model open and active', '')}.\n- The bridge builds only in a {e.doc} it made (`{tool(e, 'new')}`).\n- One feature per `{tool(e, 'add')}` call; it checks the feature and undoes it\n  if the check fails.\n- Dimensions in millimetres. Fillet and chamfer edges are named by a point\n  mid-edge.\n- Sketch, extrude, cut, fillet and chamfer only. A thread, loft or sheet-metal\n  feature is hand-entered beside the model, and said so.\n- **Building through the {N} bridge is unverified**: it has not been run\n  against a live {N} {'session' if k == 'solidworks' else 'account'}. Say so at Gate 3.\n\nThen:"),
            ("document lists none. Anything there means the add-in is replaying an earlier\n   script: stop and ask the user to restart Fusion.",
             f"{e.doc} lists none. Anything there: stop, and tell the user what was found."),
            ("   this line. Record both lists. Tell the user to restart Fusion.",
             "   this line. Record both lists. Tell the user what was found."),
            ("1. **The user opens the prior model in Fusion.** Read it, read-only: name,\n   reference, version, parameters, bodies, sketches, unsaved changes.",
             f"1. **{e.open_prior}** Read it, read-only: name,\n   reference, {e.version_word}, parameters, features, unsaved changes. Protect it."),
            ("Without Fusion: the hand-entered feature list, prefilled from the text, every\nrow `from_text`, `origin: hand_entered`. Still a ROM.",
             f"Without {N}: the hand-entered feature list, prefilled from the text, every\nrow `from_text`, `origin: hand_entered`. Still a ROM."),
            ("**STEP import through the connector is unverified.** It has never been run in\nthis repo. Say so on the line, every time.\n\nWith Fusion: import the STEP into a new document named like any estimating\nmodel, integrity check before and after, then read bodies, bounding box,\nvolume and faces read-only.",
             f"**STEP import through the {N} bridge is unverified.** It has not been run\nagainst a live {N} {'session' if k == 'solidworks' else 'account'}. Say so on the line, every time.\n\nWith {N}: import the STEP with `{tool(e, 'import')}` into a new {e.doc} named\nlike any estimating model, integrity check before and after (`{tool(e, 'list')}`),\nthen read bodies, bounding box, volume and faces read-only (`{tool(e, 'faces')}`)."),
            ("`origin: customer_step`, `cad_backend: fusion`.",
             f"`origin: customer_step`, `cad_backend: {k}`."),
            ("**When the import fails or Fusion is not connected**, in this order:",
             f"**When the import fails or {N} is not connected**, in this order:"),
            ("## When Fusion is not there\n\nThe chain completes; step 7 changes shape, and says so in one line on the\ninternal sheet: *\"Fusion not connected — no model built.\"*",
             f"## When {N} is not there\n\nThe chain completes; step 7 changes shape, and says so in one line on the\ninternal sheet: *\"{N} not connected — no model built.\"*"),
            ("Either way the chain does not stop at step 7 for want of Fusion.",
             f"Either way the chain does not stop at step 7 for want of {N}."),
        ],
        # ------------------------------------------------------------------ quote-build
        "skills/quote-build/SKILL.md": [
            ("and Autodesk Fusion for CAM-derived hours where connected.",
             "and the confirmed geometry estimate for feature-derived hours (this edition has no CAM connection)."),
            ("the CAM path where Fusion\n   and plant cutting data exist, the feature path always.",
             "the feature path (the CAM\n   path is not connected in this edition)."),
            ("(`fusion_default`) are rung 4,", "(`cam_library_default`) are rung 4,"),
            ("`estimator_entered`, `fusion_default`, library defaults)", "`estimator_entered`, `cam_library_default`, library defaults)"),
            ("| Fusion, or its Manufacturing workspace | Feature path only, and the internal sheet says so. No toolpath check is claimed |",
             "| CAM | Not connected in this edition. Feature path only, and the internal sheet says so. No toolpath check is claimed |"),
            ("| Plant cutting data | The CAM path runs on library feeds, labelled `fusion_default`, rung 4. The feature path cannot time a cut; that op is rung 4 |",
             "| Plant cutting data | The feature path cannot time a cut; that op is rung 4 |"),
        ],
        "skills/quote-build/reference/hours-ladder.md": [
            ("routing from one Fusion produced with library feeds,", "routing from one CAM produced with library feeds,"),
            ("or a line `Fusion tool library: <name>` | The CAM path runs on library feeds and every op is `fusion_default`, rung 4. The feature path cannot compute a cut time |",
             "or a line `CAM tool library: <name>` | The feature path cannot compute a cut time |"),
            ("| Anything on `fusion_default` cutting data.", "| Anything on `cam_library_default` cutting data."),
            ("Runs when Fusion is connected with its Manufacturing workspace, a confirmed\ngeometry estimate has a `document_ref`, and cutting data exists in the Plant\ncontext or as a named Fusion tool library.",
             "**Not available in this edition: no CAM system is connected.** The section\nstays so the sheet's columns and labels match the other editions. It would run\nwhen a CAM connection exists, a confirmed\ngeometry estimate has a `document_ref`, and cutting data exists in the Plant\ncontext or as a named CAM tool library."),
            ("(the Plant context table, or the named Fusion tool library),", "(the Plant context table, or the named CAM tool library),"),
            ("   `fusion_default`. **`fusion_default` demotes", "   `cam_library_default`. **`cam_library_default` demotes"),
            ("Without Fusion Manufacturing: the feature path only, and the sheet says so.",
             "Without a CAM connection — always, in this edition: the feature path only, and the sheet says so."),
            ("Runs always where a confirmed geometry estimate exists, Fusion or not.",
             "Runs always where a confirmed geometry estimate exists, from CAD or hand-entered."),
            ("| `plant_library`, `estimator_entered`, `fusion_default`, or n/a |", "| `plant_library`, `estimator_entered`, `cam_library_default`, or n/a |"),
        ],
        # ------------------------------------------------------------------ contracts
        "shared/contracts/geometry-estimate.md": [
            ("**It must be hand-authorable.** Where Fusion is not connected,",
             f"**It must be hand-authorable.** Where {N} is not connected, or cannot build the path,"),
            ("| The new Fusion document the model lives in.", f"| The new {N} {e.doc} the model lives in."),
            ("| `fusion` or `none`. `none` on `hand_entered`", f"| `{k}` or `none`. `none` on `hand_entered`"),
            ("The Fusion connector can replay an earlier script into every later call until\nFusion restarts, which puts geometry nobody asked for into the document. So",
             f"A write that lands in the wrong {e.doc} is the failure that costs most. So"),
            ("| Names as Fusion listed them |", f"| Names as {N} listed them (features and bodies) |"),
            ("PN-4471-B Rev 2, path C2, built in Fusion from the drawing.",
             f"PN-4471-B Rev 2, path C2, built in {N} from the drawing."),
            ('  document_ref: "fusion://Fairview/estimates/EH-RFQ-0912_L1_PN-4471-B_R2"', f'  document_ref: "{e.example_estimate_ref}"'),
            ("  cad_backend: fusion", f"  cad_backend: {k}"),
            ('  base_model_ref: "fusion://Fairview/parts/PN-4402-A v7 (version read again after the run: v7)"', f'  base_model_ref: "{e.example_prior_ref}"'),
            ('  document_ref: "fusion://Fairview/estimates/EH-RFQ-0930_L1_PN-4402-C_R1"', f'  document_ref: "{e.example_adapted_ref}"'),
        ],
        "shared/contracts/variant-manifest.md": [
            ("| `fusion` or `none`. See the adapter note |", f"| `{k}` or `none`. See the adapter note |"),
            ("Fusion is the one implementation behind the interface today.", f"{N} is the one implementation behind the interface in this edition."),
            ('  document_ref: "fusion://Fairview/BracketUpper/v7"', f'  document_ref: "{e.example_ref}"'),
            ("cad_backend: fusion", f"cad_backend: {k}"),
            ('    document_ref: "fusion://Fairview/BracketUpper-V001/v1"', f'    document_ref: "{e.example_variant_ref}"'),
            ('    document_ref: "fusion://Fairview/BracketUpper-V002/v1"', f'    document_ref: "{e.example_variant_ref_2}"'),
            ("| `cad_backend` holds a value other than `fusion` or `none` (an older manifest) |",
             f"| `cad_backend` holds a value other than `{k}` or `none` (a manifest from another edition) |"),
        ],
        # ------------------------------------------------------------------ onboarding and connectors
        "skills/onboarding/SKILL.md": [
            ("(Microsoft 365, Oracle NetSuite, Infor M3, Autodesk Fusion)", f"(Microsoft 365, Oracle NetSuite, Infor M3, {N})"),
            ("Tests Microsoft 365, Oracle NetSuite, Infor M3 and Autodesk Fusion.", f"Tests Microsoft 365, Oracle NetSuite, Infor M3 and {N}."),
            ("365, Oracle NetSuite, Infor M3 and Autodesk Fusion.** Only Fusion is declared", f"365, Oracle NetSuite, Infor M3 and {N}.** Only {N} is declared"),
            ("runs — SAP, SolidWorks, Limble, a homegrown MES —", "runs — SAP, Creo, Limble, a homegrown MES —"),
            ('- **Anything else** — *"No supported connector for SolidWorks.', '- **Anything else** — *"No supported connector for Creo.'),
            ("Fusion tool library; the variance-flag threshold;", "CAM tool library; the variance-flag threshold;"),
        ],
        "skills/onboarding/reference/config-profile.md": [
            ("- **Connectors not live:** Autodesk Fusion", f"- **Connectors not live:** {N}"),
            ("Microsoft 365, Oracle NetSuite, Infor M3 and Autodesk Fusion can be `live (supported)`;", f"Microsoft 365, Oracle NetSuite, Infor M3 and {N} can be `live (supported)`;"),
            ("- **CAD:** SolidWorks - file", f"- **CAD:** {N} - file"),
            ("- **Fusion tool library:** not used", "- **CAM tool library:** not used"),
            ("- **Cutting data** is either the table or a named Fusion tool library — record\n  `Fusion tool library: <name>` instead of the table when that is where the\n  plant keeps it. Neither means hours derived in Fusion run on library\n  defaults,",
             "- **Cutting data** is either the table or a named CAM tool library — record\n  `CAM tool library: <name>` instead of the table when that is where the\n  plant keeps it. Neither means hours derived from geometry run on library\n  defaults,"),
            ("| Fusion live, or a CAD connector live (custom) |", f"| {N} live, or a CAD connector live (custom) |"),
        ],
        "skills/onboarding/reference/connector-options.md": [
            ("NetSuite, Infor M3, Autodesk Fusion).", f"NetSuite, Infor M3, {N})."),
            ("| SolidWorks | No vendor MCP found | Export (already running) | Nothing needed; revisit if a vendor MCP appears |",
             "| Creo | No vendor MCP found | Export (already running) | Nothing needed; revisit if a vendor MCP appears |"),
        ],
        "skills/onboarding/reference/interview.md": [
            ("Infor M3, Autodesk Fusion) — test it now.", f"Infor M3, {N}) — test it now."),
            ("Or the name of the plant's Fusion tool library, if", "Or the name of the plant's CAM tool library, if"),
        ],
        "skills/custom-connector/reference/known-systems.md": [
            ("| Autodesk Fusion | 1 — supported | First-party integration. Desktop app only |",
             (f"| SOLIDWORKS | 1 — supported | Bundled bridge (`servers/solidworks_bridge.py`), Windows only, SOLIDWORKS running. No vendor-published MCP |\n"
              f"| Autodesk Fusion | 1 — vendor MCP, added via custom-connector | Desktop app only. Not used by this edition's skills |")
             if k == "solidworks" else
             (f"| Onshape (REST API) | 1 — supported | Bundled bridge (`servers/onshape_bridge.py`) with the plant's API keys |\n"
              f"| Autodesk Fusion | 1 — vendor MCP, added via custom-connector | Desktop app only. Not used by this edition's skills |")),
            ("Oracle NetSuite, Infor M3 and Autodesk Fusion. These are the supported\nconnectors; only Fusion is declared in the plugin's `.mcp.json`.**",
             f"Oracle NetSuite, Infor M3 and {N}. These are the supported\nconnectors; only {N} is declared in the plugin's `.mcp.json`, as its bundled bridge.**"),
            ("**No skill in this plugin uses it:** `design-space-explore` and `rfq-to-quote` are built on Fusion, so connecting Onshape enables nothing today — say so |",
             ("**No skill in this plugin uses it:** this is the SOLIDWORKS edition; the Onshape edition of this plugin reaches Onshape through its own bridge — say so |"
              if k == "solidworks" else
              "**It does not replace the bundled bridge:** it writes FeatureScript custom features; `design-space-explore` and `rfq-to-quote` read and vary models through the bridge. Connect it only to author custom features |")),
            ("| **Autodesk Fusion** | In the Fusion **desktop** app: *Preferences → General → API* → enable **Fusion MCP Server**. It runs locally on your machine and needs Fusion open with a document. If you changed the port from 27182, set `FUSION_MCP_PORT` |",
             ("| **SOLIDWORKS** | On the Windows machine running SOLIDWORKS: install Python 3.10+ and run `pip install pywin32`. The plugin starts the bridge itself. If the Python launcher is not `python`, set `SOLIDWORKS_BRIDGE_PYTHON` (for example `py`). Open SOLIDWORKS with the model before asking |"
              if k == "solidworks" else
              "| **Onshape** | Create an API key in Onshape (*My Account → Developer*, or *Company Settings → Developer*). Set `ONSHAPE_ACCESS_KEY` and `ONSHAPE_SECRET_KEY` in the environment Claude starts from — never in the conversation. Enterprise stacks also set `ONSHAPE_BASE_URL`. Needs Python 3.10+; set `ONSHAPE_BRIDGE_PYTHON` if the launcher is not `python3` |")),
        ],
        "shared/approval-gates.md": [
            ("  Slack or Fusion, approval or not,", f"  Slack or {N}, approval or not,"),
            ("Fusion has one narrow\n  exception", f"{N} has one narrow\n  exception"),
        ],
        "README.md": readme_edits(e),
        "shared/secret-config.md": [
            ("connector's settings (`${FUSION_MCP_PORT}`),",
             "connector's settings (`${SOLIDWORKS_BRIDGE_PYTHON}`),"
             if k == "solidworks" else
             "connector's settings and credentials (`${ONSHAPE_ACCESS_KEY}`, `${ONSHAPE_SECRET_KEY}`),"),
        ],
    }


# ---------------------------------------------------------------------------


def safety_edits(e: Edition) -> Dict[str, List[Tuple[str, str]]]:
    """Edits from the Nov 14 review: say what the bridges can and cannot check, and label a
    failed adaptation as what it is. Applied after `edits`, to the text those edits produced."""
    N, k = e.name, e.key
    list_tool = tool(e, "list")
    integrity_before = (
        f"List features, sketches and bodies in the new {e.doc} (`{list_tool}`), read-only.\n"
        f"**They must match the prior model's list from step 1, name for name.** A new\n"
        f"{e.doc} that is a copy is not empty, so \"a fresh {e.doc} lists none\" does not\n"
        f"apply here; \"it lists exactly what the prior model held\" does. Anything else\n"
        f"is unexpected content: the copy is not the one expected, or something else\n"
        f"wrote to it. Stop, and tell the user what was found.\n\n"
        f"Record `document_integrity.result: clean` only when both reads returned the\n"
        f"bodies as well as the features and they match. Where {N} returned no body\n"
        f"list, the result is `not_checked`, with the reason."
    )
    return {
        "skills/design-space-explore/reference/adapt-prior-model.md": [
            ("- its bodies and sketches, by name",
             f"- its features, sketches and bodies, by name (`{list_tool}`), and whether it\n"
             "  has external references"),
            ("List bodies and sketches in the new document, read-only. **They must match the\nprior model's list from step 1, name for name.** A new document that is a copy\nis not empty, so \"a fresh document lists none\" does not apply here; \"it lists\nexactly what the prior model held\" does. Anything else is foreign geometry: the\ncopy is not the one expected, or something else wrote to it. Stop, and\ntell the user what was found.",
             integrity_before),
            ("Same bodies and sketches by name, plus only what this step created. Anything\nelse is foreign geometry and the step fails, with both lists recorded.",
             f"Same features, sketches and bodies by name (`{list_tool}`), plus only what this\n"
             "step created. Anything else is unexpected content and the step fails, with\n"
             "both lists recorded. As at step 3, the result is `clean` only when both reads\n"
             "returned bodies."),
            ("Apply each changed parameter. After the set, confirm the model regenerated\nwith no failed feature.",
             "Apply the whole parameter set in one call: the bridge checks every change\n"
             "before it writes any, and undoes the set if one fails part way. Then confirm\n"
             "the model regenerated with no failed feature. **Only `regenerated: true` is a\n"
             "pass**: false is a failure, and a missing or unknown health is treated as one."),
            ("the regeneration result, both\nintegrity lists, and the untouched check.",
             "the regeneration result, both\nintegrity lists, the integrity result (`clean`, or `not_checked` with the reason),\n"
             "and the untouched check."),
        ],
        "skills/design-space-explore/reference/parametric-check.md": [
            ("  decide whether to proceed with that risk understood rather than discover\n  a broken assembly reference after the batch runs.",
             "  decide whether to proceed with that risk understood rather than discover\n  a broken assembly reference after the batch runs.\n"
             f"  The {N} bridge reads them with `{list_tool}` (unverified), and says what it\n"
             "  scanned. Record `true` or `false` as it returns them; where it cannot\n"
             "  tell, record `external_references_present: unknown`, never `false`, and\n"
             "  tell the engineer before generating that they were not checked."),
        ],
        "skills/design-space-explore/SKILL.md": [
            ("- **The model has external references or in-context relationships** —\n  variant generation can break them. Detect and warn before generating.",
             "- **The model has external references or in-context relationships** —\n  variant generation can break them. Detect them with `"
             + list_tool + "` and warn\n  before generating. Where the bridge cannot tell, say they were not checked,\n  and ask."),
        ],
        "skills/design-space-explore/reference/variant-generation.md": [
            ("**Generation is idempotent.** Before creating a variant, look for an existing\ndocument with the same name and the same parameter values: after a partial\nbatch, a re-run, or a call that executed twice. If one exists, reuse it and\nrecord it in the manifest as `reused_existing` with its reference; never create\na second copy.",
             "**Generation is idempotent.** Before creating a variant, look for an existing\n"
             "one with the same name and the same parameter values: after a partial\n"
             f"batch, a re-run, or a call that executed twice. **The {N} bridge cannot\n"
             f"search for one**, so look in this study's manifest, which records each\n"
             f"{e.doc} as it is made. If the manifest has it, reuse it and record it as\n"
             "`reused_existing` with its reference; never create a second copy. If there is\n"
             "no manifest from the earlier run, say so and ask the engineer before making\n"
             "new copies"
             + (" (the bridge already refuses a file name that exists)." if k == "solidworks" else ".")),
        ],
        "shared/contracts/variant-manifest.md": [
            ("  external_references_present: true\n```",
             "  external_references_present: true\n```\n\n"
             f"`external_references_present` is `true`, `false` or `unknown`. The {N} bridge\n"
             f"reads it with `{list_tool}`; `unknown` where it could not tell, never `false`."),
        ],
    }


def apply_edits(target: Path, edition: Edition) -> List[str]:
    problems = []
    combined = edits(edition)
    for rel, pairs in safety_edits(edition).items():
        combined[rel] = combined.get(rel, []) + pairs
    for rel, pairs in combined.items():
        path = target / rel
        if not path.exists():
            problems.append(f"{rel}: file not found")
            continue
        text = path.read_text("utf-8")
        for old, new in pairs:
            count = text.count(old)
            if count != 1:
                problems.append(f"{rel}: expected 1 match, found {count}: {old[:80]!r}")
                continue
            text = text.replace(old, new)
        path.write_text(text, "utf-8")
    return problems


def overlay(target: Path, edition: Edition) -> None:
    common = HERE / "common"
    source = HERE / "overlays" / edition.key
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    shutil.copy2(common / "mcp_stdio.py", target / "servers" / "mcp_stdio.py")
    shutil.copy2(HERE / "NOTICE", target / "NOTICE")


FUSION = re.compile(r"(?<![a-z])fusion", re.IGNORECASE)


def leftover_fusion(target: Path) -> List[str]:
    found = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.suffix in (".png", ".pyc"):
            continue
        rel = path.relative_to(target).as_posix()
        allowed = ALLOWED_FUSION_LINES.get(rel, [])
        for number, line in enumerate(path.read_text("utf-8", errors="replace").splitlines(), 1):
            if FUSION.search(line) and not any(a in line for a in allowed):
                found.append(f"{rel}:{number}: {line.strip()[:120]}")
    return found


def build(edition: Edition, out_root: Path) -> List[str]:
    target = out_root / edition.plugin
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(UPSTREAM, target, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    problems = apply_edits(target, edition)
    overlay(target, edition)
    problems += [f"Fusion still mentioned: {line}" for line in leftover_fusion(target)]
    return problems


def same_tree(a: Path, b: Path) -> bool:
    comparison = filecmp.dircmp(a, b, ignore=["__pycache__"])
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, comparison.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(same_tree(a / d, b / d) for d in comparison.common_dirs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if the committed editions differ from a fresh build")
    parser.add_argument("--edition", choices=sorted(EDITIONS), action="append", help="build only this edition")
    args = parser.parse_args()
    chosen = [EDITIONS[k] for k in (args.edition or sorted(EDITIONS))]

    failed = False
    for edition in chosen:
        if args.check:
            with tempfile.TemporaryDirectory() as tmp:
                problems = build(edition, Path(tmp))
                fresh = Path(tmp) / edition.plugin
                committed = ROOT / "plugins" / edition.plugin
                if not problems and not (committed.exists() and same_tree(fresh, committed)):
                    problems.append(f"plugins/{edition.plugin} differs from a fresh build; run build.py and commit")
        else:
            problems = build(edition, ROOT / "plugins")
        if problems:
            failed = True
            print(f"{edition.plugin}: FAILED", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
        else:
            print(f"{edition.plugin}: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
