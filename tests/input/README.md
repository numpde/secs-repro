# Omni-parser acceptance tests

These are tests-first requirements for design note 007. Production still uses
the legacy readers. Missing behavior must fail; do not add skips or translate
the requests back to legacy readers in test helpers.

`make test/input` runs the input suite in the offline CPU image; the initial
slice is normative discovery (`make test/input/normative`). Adversarial and
selected-input tests follow in separately reviewed slices. There is no checkpoint or index;
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
contract will be tested with its first selected-input consumer.

All alternatives from each inspected Upload remain visible. A localized parse
failure may coexist with useful choices; it cannot be reported as an exhaustive
empty inventory. Insufficient inspection budgets must be explicit. A supplied
file's title, extension or description cannot override contradictory scientific
metadata. Explicit formula instructions do not establish molecular identity.

## Evidence and scope

Fixtures in `/fixtures/input` are newly authored mathematical signals and
structure tables, licensed AGPL-3.0-only under the repository LICENSE. Their
per-file provenance, hashes, derivations and reference settings are recorded in
`tests/fixtures/input/provenance.json`. Runtime archive wrappers and mutations
retain their parent fixture's licence; the test describes the transformation.
The generator verifies its reference source hashes before writing.
`make fixtures/input/write` builds the pinned reference image, generates into
private staging without runtime network, and publishes only after generation
succeeds. Building that image may need dependency access; ordinary tests and
generation runtime are offline. Review regenerated differences explicitly.
Ordinary tests never update goldens and check recorded artifact integrity.

The corpus includes reference vectors for later preparation tests. This
initial slice checks discovery and fixture integrity; it does not yet establish
numerical agreement or independent peak-position correctness.
Reference generation does not prove all vendor variants work, and repeated
failure at missing discovery does not exercise assertions later in a test.
Scripted interpreter replies prove transport/orchestration, not judgment.
Real-LLM and deployed GUI/API qualification remain separate roadmap obligations.
