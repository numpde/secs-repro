# SDK acquisition bootstrap

The acquisition directory contains the source bundle from the SDK release
identified by `contracts/upstream/provider_core_release.json`. It is available
before the shared wheelhouse is populated and serves both acquisition and the
existing BuildKit proxy path. Run `python3 -B tools/acquisition_bootstrap.py`
to verify its release inventory and provider-core lock pins offline. The
checked-in issuer record is not an independent signature.

When no acquisition or BuildKit proxy is using the bundle, install or refresh
from the exact release archive by running
`python3 -B tools/acquisition_bootstrap.py --archive PATH`. These files are
generated release material; do not edit their implementation locally.
