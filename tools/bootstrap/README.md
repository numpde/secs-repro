# Acquisition source

`tools/bootstrap/acquisition` is the source archive selected by
`requirements/provider-tools-source.json`. Its embedded manifest records the
payload inventory and release provenance. Runtime SDK locks and their issuer
record remain independent inputs.

Run `make provider-tools/verify-checked-in-source-hashes` to check the snapshot
offline. With acquisitions and builds quiescent, use
`make provider-tools/update-checked-in-source-from-pinned-release` to download
one archive from the pinned GitHub release, or
`make provider-tools/import-checked-in-source INPUT_RELEASE_ARCHIVE=/absolute/path/acquisition-owner-VERSION.tar.gz`
to import the same archive offline. The checked-in updater verifies the existing
snapshot before replacement and the reviewed archive digest before parsing it.
These files are generated release material; do not edit them locally.
