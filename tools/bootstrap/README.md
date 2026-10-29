# SDK proxy bootstrap

The files under `provider_core/` are unchanged source from the provider-core
release wheel identified by `contracts/upstream/provider_core_release.json`.
They provide a cold-start copy of the shared proxy without requiring an SDK
checkout or a prior wheel download. The checked-in release record is the
repository's pinned issuer record, not an independent signature.

After updating that record for a new SDK release, regenerate all three files
from its exact wheel with `python3 tools/bootstrap_snapshot.py --wheel PATH`.
Run `python3 tools/bootstrap_snapshot.py` to verify their sizes and digests
offline. Treat the record and all three files as one release update. This copy
is generated release material; do not edit its implementation locally.
