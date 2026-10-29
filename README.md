# SECS, repackaged

SECS deployed as an NMR API provider.

[Documentation, attribution, and license](https://numpde.github.io/secs-repro/).

The provider uses the shared [provider-core SDK](https://github.com/numpde/nmr-provider-tools/tree/main/packages/provider-core)
for Attempt control, verified transfers, native supervision and durable delivery.
Select its operator-owned binding definition in `config/provider.toml.example`.
The launcher projects the binding's private API proxy; the provider requires
that route. `network.toml` separately controls interpreter and upload-store egress.
The API and scientific images install the same SDK wheel using its issued API
and native installation locks; the scientific image retains its own dependency pins.

`make provider/deployment/down DEPLOYMENT=<name>` stops admission and waits for
work and selected reports before stopping the scientific worker. It may wait
indefinitely while delivery is unavailable; interruption leaves shutdown unconfirmed.

Before switching a legacy deployment, drain its obligations under the old runtime
and preserve its evidence. Before starting this volume-backed deployment, move
the stopped deployment's old `state`, `sources`, and `socket` directories out of
its runtime directory; startup refuses to silently leave those owners behind.
[Legacy recovery instructions](notes/005_closed_attempt_archival.txt) apply to
the old runtime. Provider lifetime, source files, worker IPC and API routing use
Docker-managed volumes; the shared delivery queue remains separately owned.

[GPU selection and checks after host software upgrades](notes/004_provider_execution.txt).

[Terminal outcome policy](src/secs_inference/provider/outcomes.py).
