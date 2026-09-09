# SECS, repackaged

[Read the user guide](https://numpde.github.io/secs-repro/): supported uploads,
job instructions, results, and limitations.

This repository repackages [SECS](https://github.com/lamalab-org/secs) for the
NMR API. The scientific methods and pretrained model are upstream work—not
methods developed here. Our contribution is input conversion, API integration,
reproducible packaging, and deployment support, with adaptations in pinned forks.

Please credit Adrian Mirza and Kevin Maik Jablonka,
[Elucidating structures from spectra using multimodal embeddings and discrete
optimization](https://doi.org/10.26434/chemrxiv-2024-f3b18-v2) (2024), and follow
the [upstream citation guidance](https://github.com/lamalab-org/secs#citation).

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
