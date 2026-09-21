# Omni-parser acceptance tests

These tests define the current input-adapter contract from
[design note 007](../../notes/007_omni_parser_tests_first_20260921.txt).
The scientific worker uses this adapter for discovery and execution. Missing
behavior must fail; do not add skips or reintroduce format-specific request
contracts in test helpers.

`make test/input` runs the input suite in the offline CPU image. Normative
discovery and adversarial checks can run separately with
`make test/input/normative` and `make test/input/adversarial`.
`make test/input/scenarios` checks multi-input interactions at the scientific
worker: an explicit formula alongside an unrelated structure, and correction
followed by execution from a partial inventory. These are not API/GUI flows.
Selection tests observe the prepared Float32 spectrum at the worker's inference
port. One representative case continues through the real inference adapter to
the model call, while a focused integration test owns normalization, training
order and Torch conversion. There is no checkpoint or index;
the current scientific package's eager imports require the existing verified
MolFormer configuration/tokenizer cache. No model weights are loaded. This is
a temporary import dependency, not a reason to mock scientific input handling.
Prepare missing prerequisites with
`make packages/base-images/pull packages/cpu/wheelhouse molformer/cache`;
this preparation may use network access. No checkpoint is required.

## Consumer contract

The existing worker `inspect` request identifies an Upload or exact ZIP member.
Discovery must not start inference or candidate retrieval; invalid selections
must fail before either operation.
Its `facts` should contain `representations`, `complete` and localized `issues`.
Each representation has an opaque `id`, `kind`, exact `sources` (Upload/member
pairs), scientific `metadata`, and `related_ids` where relationships are known.
Metadata assertions concern observed nucleus, dimensionality, point count,
frequency, peak-table columns or structure formula as appropriate. Missing
evidence must remain unknown, not become a default scientific fact.

`complete` describes the inventory within the inspected source scope. It is
true when discovery has exhaustively classified every content-recognized
representation and required resource in that scope, including when no supported
representation exists. It does not mean the Job is analysable, every metadata
field is known, or every representation is suitable for SECS. Unknown unrelated
content is ignored; a filename or extension alone does not establish a malformed
supported input. Recognized malformed input or an unresolved required resource
produces an issue for that source and makes the inventory incomplete.

Archive-root inspection covers all admitted regular members. Exact-member
inspection begins with that member and includes only same-Upload resources that
the format explicitly associates with it; unrelated siblings do not affect that
inventory. Archive parsing limits, including the central-directory member cap,
apply before exact member access. Upload namespaces never supply one another's resources. A source
admission failure returns `input_rejected` before inventory facts exist, while
operational storage failures remain operational errors. If an implementation
introduces further inspection limits, their effect needs its own acceptance
contract rather than an implicit interpretation of `complete`.

Every issue identifies the affected Upload/member and carries a nonblank,
printable reason without private workspace paths. The reason names the evidence
that blocks inspection or execution, such as an incomplete dataset or the exact
missing companion. Tests assert those durable facts within one attributed issue;
they do not prescribe parser tokens or a machine category without a consumer.

An ID must identify the exact representation in the current acquired source
set; its spelling is not prescribed. These are internal discovery requirements,
not the reference frontend's response schema. The execution
request uses
`selection={representation_id, formula, formula_evidence, processing, explanation}`.
Formula evidence names either the admitted Job specification or one or more
discovered structure representations. It makes the interpreter's source
auditable; explanation prose cannot substitute for it.
There is no format-specific reader discriminator. The opaque identity must
survive separate inspect/analyse calls for unchanged acquired sources; removed
or changed sources invalidate it, and it is valid only in the Attempt that
issued it. Processing is explicit: `as_stored` for
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
NMReDATA annotations accompany the spectrum as `metadata.annotations`, with
supplied `shift`, `multiplicity`, `atom_count` and `assignment` evidence; existing
`related_ids` identify the associated structure. Assignments retain their
source `label` and `atoms`, so swapped assignments cannot pass merely because
both rows contain an opaque identifier. These labels
record the supplied annotation; they do not validate the molecule's identity.
The `N` declarations are atom counts, even though the reference uses them for
display integration.
They do not establish measured integrals. Missing relative resources remain
unresolved across Upload namespaces; annotation rows are not dense spectra.

All alternatives in the inspected source scope remain visible. A localized parse
failure may coexist with useful choices; it cannot be reported as an exhaustive
empty inventory. A supplied
file's title, extension or description cannot override contradictory scientific
metadata. Explicit formula instructions do not establish molecular identity.
Direct acquired Uploads do not retain an authoritative filename. Standalone
format recognition must therefore use contents, including JEOL, NMRium and
structure files; archive companion paths retain their separate meaning.

## Evidence and scope

Locally authored fixtures in `tests/fixtures/input` (mounted at `/fixtures/input`
inside the test container) are mathematical signals and
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
succeeds. It requires the pinned commit in `FRONTEND_REFERENCE_REPOSITORY`
(default `../fork-of-elucidation.cheminfo.org`); dependency preparation does not
create that checkout. Building the image may need dependency access; ordinary tests and
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
suite checks discovery, fixture integrity and exact prepared spectra at the
worker's inference port.
Reference comparisons allow two Float32 ULPs. The only observed second-ULP
difference is one point in the magnitude-FID vector produced by the pinned
JavaScript reference and NumPy normalization paths.
One representative selection continues through the model adapter and checks an
independently known peak position; the complete parsing and preparation matrix
remains at the inference port.
Adversarial tests check worker source rejection, operational-error propagation,
partial discovery and misleading metadata. Instruction-like titles test parser
facts; they do not establish resistance to prompt injection in a live interpreter.
Reference generation does not prove all vendor variants work, and repeated
failure at missing discovery does not exercise assertions later in a test.
The concrete Chat Completions contract lives under `input/adapters`; a future
transport replaces that adapter contract rather than redefining scientific
selection. Scripted interpreter replies prove transport/orchestration, not judgment.
Real-LLM and deployed GUI/API qualification remain separate roadmap obligations.

## Coverage map and qualification status

This map names the behavior exercised by the current suite.

| Input or boundary | Requirements in this suite |
| --- | --- |
| JCAMP | AFFN, FIX, SQZ, DIF, DIFDUP, PAC; ascending axes; complex channels; LINK alternatives and peak tables; 2D discovery and SECS refusal |
| Bruker, Varian, JEOL | Raw Bruker/Varian and processed Bruker/JEOL discovery; companion ownership, multiple experiments and pdata directories, incomplete datasets, nameless JEOL content |
| NMRium | Multiple nuclei, stored shift replay, native embedded resources, unresolved URL resources and observed no-fetch behavior |
| MOL, SDF, SMILES, NMReDATA | Structure formula evidence, separate records, explicit annotation relationships, supplied counts/assignments, missing resources |
| Multiple Uploads and archives | Order independence, exact member choices, duplicate bytes/names, separate namespaces, partial inventories |
| Selection and preparation | Opaque selection identity, stale/removed sources, explicit formula/processing, unsuitable-data refusal, selected prepared spectra, FID magnitude reporting |
| Interpreter and scenarios | Analysis-kind authority, all admitted Upload metadata, selection-tool transport, explicit formula with unrelated evidence, correction before execution |
| Source failures | Membership, missing/duplicate/nonregular members, archive limits, storage faults, localized malformed data and misleading metadata |

Passing fixture checks verify recorded attribution and byte integrity;
explicit generation separately verifies reference admission. Neither establishes
parser parity. Automatic JCAMP, Bruker and Varian FID preparation is checked
numerically at the worker inference port. Raw Bruker is limited to the qualified
zero group-delay profile; raw Varian is limited to the qualified
centered-reference profile. The JEOL specimen is reference-compatible synthetic
data, not instrument qualification. The user's original JDX is a separate local
regression and has not been admitted to this shared corpus. Additional
instrument variants, live interpreter judgment and deployed GUI/API flows
remain separate qualification work.
