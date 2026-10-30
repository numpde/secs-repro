# SECS, repackaged

SECS deployed as an NMR API provider.

[Documentation, attribution, and license](https://numpde.github.io/secs-repro/).

The provider uses the shared [provider-core SDK](https://github.com/numpde/nmr-provider-tools/tree/main/packages/provider-core)
for Attempt control, verified transfers, native supervision and durable delivery.
Each named deployment keeps its shared socket grant in
`config/deployments/<name>/provider.toml` under `[shared]`: set the project name
and socket group ID to the values used by the shared services. These select the
API proxy and outbox volumes. The same file selects that provider's API identity.
The grant values are nonsecret; the signing credential remains in
`secrets/deployments/<name>` and is installed separately.
For an existing v3 deployment, change `schema_id` to `secs.provider.config.v4`
and replace `[shared].settings_file` with `[shared].project` and `[shared].gid`
before rendering or starting it. Keep the existing credential files unchanged.
The launcher borrows the independently managed API proxy and outbox sockets.
`network.toml` separately controls interpreter and upload-store egress.
The API and scientific images install the same SDK wheel using its issued API
and native installation locks; the scientific image retains its own dependency pins.
Select a shared package cache with `WHEELHOUSE=/path/to/wheelhouse` for dependency
acquisition and image builds. Unattended commands require an explicit path;
interactive commands can prompt. Builds stage the locked inputs and install offline.
Initialize a named deployment with `TARGET=cpu` or `TARGET=gpu`; an interactive
terminal offers the choice when omitted. Build and select the corresponding CPU
or GPU packages image. GPU deployments also require the host GPU UUID selection.
For deployment checks, `make provider/deployment/init DEPLOYMENT=<name> TARGET=cpu
USE_MOCK_INDEX=yes` selects a real model with an eight-molecule starting index.
Its advertised offering and results identify the limited index; it does not
represent a full scientific search. Omitting the flag selects the full index.
Alternatively, initialize with `TARGET=cpu USE_MOCK_RUNNER=yes` to return a
clearly marked mock result without an interpreter, model, or candidate files.
The mock runner and mock index cannot be selected together.
The mock runner is selected during initialization. For a deployment initialized
with a full CPU worker, pass `USE_MOCK_INDEX=yes` to `provider/deployment/config`
or `provider/deployment/up` to select the limited index for that invocation.
Omitting the flag uses the saved worker configuration. Stop a running deployment
before starting it with a different selection.
For a deployment initialized before this option was added, create the private
settings file once with `cp --no-clobber config/worker.mock-index.toml.example
config/deployments/<name>/worker.mock-index.toml`, then set its mode to `0600`.
Mock modes can consume Jobs already queued for the selected provider, even after
its mock offering is advertised. A separate test provider principal or disposable
API isolates those Jobs; a separate credential for the same principal does not.
For example, initialize `localhost` with the CPU worker and use
`make provider/deployment/up DEPLOYMENT=localhost USE_MOCK_INDEX=yes` when the
limited index is wanted. Initialize `localhost-mock` with
`TARGET=cpu USE_MOCK_RUNNER=yes` and install its own mock-provider credential.
The candidate index can be built with `make candidates TARGET=cpu` or
`TARGET=gpu CANDIDATE_GPU=GPU-<uuid>`; the existing index and its evidence are
never replaced by that command.

Networked acquisition requires `NETWORK_INTERFACE=<device>` or
`NETWORK_INTERFACE=system`, supplied on the make command line or in the
environment. Without a selection, an interactive terminal offers a choice;
noninteractive acquisition fails. The device choice routes cooperating HTTP(S)
clients through the SDK proxy, which binds DNS and TCP to that device. This is
not a firewall for clients that bypass the proxy. `system` uses normal routing.
Offline operations do not ask, and Docker base-image pulls use the daemon's
default route. GitHub and provider API traffic use their normal routes.

`make provider/deployment/down DEPLOYMENT=<name>` stops admission and waits for
work and selected reports before stopping the scientific worker. It may wait
indefinitely while delivery is unavailable; interruption leaves shutdown unconfirmed.

Before switching a legacy deployment, drain its obligations under the old runtime
and preserve its evidence. Before starting this volume-backed deployment, move
the stopped deployment's old `state`, `sources`, and `socket` directories out of
its runtime directory; startup refuses to silently leave those owners behind.
[Legacy recovery instructions](notes/005_closed_attempt_archival.txt) apply to
the old runtime. Provider lifetime, source files, worker IPC and API routing use
Docker-managed volumes; the shared API route and delivery queue remain separately owned.

[GPU selection and checks after host software upgrades](notes/004_provider_execution.txt).

[Terminal outcome policy](src/secs_inference/provider/outcomes.py).
