# Omni-parser acceptance tests

These are tests-first requirements for design note 007. Production still uses
the legacy readers. Missing behavior must fail; do not add skips or translate
the requests back to legacy readers in test helpers.

`make test/input` runs the input suite in the offline CPU image. Normative
discovery and adversarial checks can run separately with
`make test/input/normative` and `make test/input/adversarial`.
`make test/input/scenarios` checks multi-input interactions at the scientific
worker: an explicit formula alongside an unrelated structure, and correction
followed by execution from a partial inventory. These are not API/GUI flows.
Selection tests observe the tensor passed to a recording encoder through the
real inference adapter. There is no checkpoint or index;
the current scientific package's eager imports require the existing verified
MolFormer configuration/tokenizer cache. No model weights are loaded. This is
a temporary import dependency, not a reason to mock scientific input handling.
Prepare missing prerequisites with
`make packages/base-images/pull packages/cpu/wheelhouse molformer/cache`;
this preparation may use network access. No checkpoint is required.

## Proposed consumer contract

The existing worker `inspect` request identifies an Upload or exact ZIP member.
Its `facts` should contain `representations`, `complete` and localized `issues`.
Each representation has an opaque `id`, `kind`, exact `sources` (Upload/member
pairs), scientific `metadata`, and `related_ids` where relationships are known.
Metadata assertions concern observed nucleus, dimensionality, point count,
frequency, peak-table columns or structure formula as appropriate. Missing
evidence must remain unknown, not become a default scientific fact.

An ID must identify the exact representation in the current acquired source
set; its spelling is not prescribed. These are proposed internal discovery
requirements, not the reference frontend's response schema. The execution
request uses `selection={representation_id, formula, processing, explanation}`.
There is no format-specific reader discriminator. The opaque identity must
survive separate inspect/analyse calls for unchanged acquired sources; removed
or changed sources invalidate it. Processing is explicit: `as_stored` for
processed data, `auto` for supported FID processing of that exact selection.
For FIDs, `auto` includes the reference's magnitude choice when phased real
data retain substantial negative intensity. Analysis preparation must report
the actual `from_fid` and `magnitude` outcomes; magnitude output must not be
presented as an absorptive spectrum. This is not an exception fallback.
No test yet prescribes `auto` behavior for already-processed data.
For NMRium, `as_stored` preserves saved processing: replay its enabled shift
exactly once, including when the original data live in a native resource archive.
A URL-only state does not authorize a fetch or establish a relationship to an
unrelated file with the same name. A loopback canary checks actual connections
during inspection, including from another process; it does not test detached
process supervision.

All alternatives from each inspected Upload remain visible. A localized parse
failure may coexist with useful choices; it cannot be reported as an exhaustive
empty inventory. Insufficient inspection budgets must be explicit. A supplied
file's title, extension or description cannot override contradictory scientific
metadata. Explicit formula instructions do not establish molecular identity.
Direct acquired Uploads do not retain an authoritative filename. Standalone
format recognition must therefore use contents, including JEOL, NMRium and
structure files; archive companion paths retain their separate meaning.

## Evidence and scope

Locally authored fixtures in `/fixtures/input` are mathematical signals and
structure tables, licensed AGPL-3.0-only under the repository LICENSE. The
unchanged upstream 4-chlorobenzylamine fixture retains its embedded public-domain
declaration; its individual creator is not stated and is recorded as unknown.
Reference vectors retain their parents' recorded rights declarations. Their
per-file provenance, hashes, derivations and reference settings are recorded in
`tests/fixtures/input/provenance.json`. Runtime archive wrappers and mutations
retain their parent fixture's licence; the test describes the transformation.
The generator verifies its reference source hashes before writing.
`make fixtures/input/write` builds the pinned reference image, generates into
private staging without runtime network, and publishes only after generation
succeeds. Building that image may need dependency access; ordinary tests and
generation runtime are offline. Review regenerated differences explicitly.
Ordinary tests never update goldens and check recorded artifact integrity.
Native NMRium archives also record every member's provenance and hash. Their
UUIDs and ZIP timestamps are canonicalized; stored processing and embedded
resource bytes are preserved. Reference settings are recorded per vector,
because direct core state loading and automatic FID preparation differ.

The Bruker and Varian companion files are stored flat in the corpus; tests
restore native experiment paths in archive wrappers. Generation verifies raw
and processed datasets with automatic processing disabled. The processed
Bruker pair-only case preserves existing behavior: the pinned reference loader
requires `acqus` too, so adopting it alone would narrow current support.
The raw and processed synthetic signals are independent examples, not a claim
that one was produced by processing the other.

The corpus includes reference vectors for selection and preparation tests. This
suite checks discovery, fixture integrity and exact selected model inputs.
Reference comparisons use the existing lanes' one-Float32-ULP allowance.
An adapter-only test checks the independently known alternate peak position
without discovery; complete parsing/preparation checks still depend on discovery.
Adversarial tests check worker source rejection, operational-error propagation,
partial discovery and misleading metadata. Instruction-like titles test parser
facts; they do not establish resistance to prompt injection in a live interpreter.
Reference generation does not prove all vendor variants work, and repeated
failure at missing discovery does not exercise assertions later in a test.
Scripted interpreter replies prove transport/orchestration, not judgment.
Real-LLM and deployed GUI/API qualification remain separate roadmap obligations.
