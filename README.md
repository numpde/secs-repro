# SECS, repackaged

[Read the user guide](https://numpde.github.io/secs-repro/): supported uploads,
job instructions, results, and limitations.

This repository repackages [SECS](https://github.com/lamalab-org/secs) for the
NMR API. The scientific methods and pretrained model are upstream work—not
methods developed here. Our contribution is input conversion, API integration,
reproducible packaging, and deployment support, with adaptations in pinned forks.

Please credit Adrian Mirza, Luc Patiny, and Kevin Maik Jablonka,
[End-to-end multimodal structure elucidation from raw spectra combining
contrastive learning and evolutionary algorithms](https://doi.org/10.1038/s41467-026-73846-y),
*Nature Communications* **17**, 5013 (2026), and follow
the [upstream citation guidance](https://github.com/lamalab-org/secs#citation).

We also used the upstream GUI, [elucidation.cheminfo.org](https://elucidation.cheminfo.org/)
([source](https://github.com/cheminfo/elucidation.cheminfo.org)), as a reference
for spectrum conversion and published challenge fixtures. Our conversion tests
use outputs from a pinned revision of its frontend. That GUI is a separate
application, not the interface to this API provider.

## License and source

[LICENSE](LICENSE) is the GNU Affero General Public License, version 3, copied
verbatim from the pinned SECS source. Its [third-party notice](open_source_licenses.txt)
is preserved too. Dependencies and external model/data artifacts retain their
own licenses; this repository does not relicense them.

The pinned SECS source is in the [secs submodule](secs); its adaptations are
maintained in [our fork](https://github.com/numpde/fork-of-secs).
The upstream package metadata says MIT while its LICENSE contains AGPLv3;
we carry the LICENSE file unchanged rather than silently resolving that discrepancy.

For development, start with `make help`. The user guide lives in
[docs/index.html](docs/index.html), published from `main` → `/docs`.
