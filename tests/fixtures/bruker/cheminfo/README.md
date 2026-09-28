# Cheminfo Bruker fixtures

Imported from [cheminfo/bruker-data-test](https://github.com/cheminfo/bruker-data-test)
at revision `89a722d085f87e4bc56f7bee9f74b007181d4bdc`.
This is Cheminfo's test collection, not an official Bruker sample release.
`SOURCE_README.md` and `LICENSE` are unchanged upstream documents.

The collection retains all 26 ZIP examples, the ibuprofen MOL file, and both
flat coffee sample groups (13 experiment directories). Upstream paths are
preserved under `data/`:

| Location | Examples |
| --- | --- |
| `data/zipped/` | Aspirin, naphtoic acid, strychnine, rubidium-87, T1rho, incomplete HSQC acquisition, TopSpin 3.6.5 and 4.5.0 |
| `data/zipped/cyclosporin/` | Proton, COSY, HMBC, HSQC, inversion recovery |
| `data/zipped/ibuprofen/` | Raw and processed proton, carbon, COSY, HMBC, HSQC; molecular structure |
| `data/zipped/nonUniformSampling/` | COSY and HSQC with nonuniform sampling |
| `data/flat/coffee/` | Two sample groups with multiple experiments and processing directories |

Names describe upstream examples; they do not establish molecular identity or
supported analysis behavior. This import provides data for future tests. It
does not add parser qualification or numerical reference results.

## License and omissions

Upstream presents its test-data collection under the MIT license, copyright
2022 Cheminfo. The retained license notice applies to this imported collection;
it does not apply to other fixtures in `tests/fixtures/bruker/`. Individual
acquisition authors are not established.

Eleven ancillary Bruker software artifacts carry explicit "All Rights Reserved"
notices without an established redistribution grant. They are omitted:

- Nine `prosol_History` files in the flat coffee experiments.
- `inversionRecovery2D/1/format.ased` inside
  `data/zipped/cyclosporin/inversionRecovery2D.zip`.
- `3/format.ased` inside `data/zipped/nonUniformSampling/hsqc-nus.zip`.

The two affected ZIPs are repacked. All retained member contents are unchanged,
including spectral data and acquisition and processing parameters. The other
24 ZIPs and every retained flat file are copied byte-for-byte.

## Provenance

`provenance.json` records each imported file's source path, SHA-256 digest and
byte length. Its shared `source` record supplies the pinned repository,
revision, license and attribution basis for every file and archive member.
Archive members also have individual hashes and byte lengths. The two repacked
archives record their original hashes and lengths; omitted files and members
have explicit paths, hashes and reasons.
The local `.gitattributes` prevents Git from normalizing fixture line endings.

To refresh, inspect the new revision's rights declarations before copying its
tracked `data/` tree. Preserve eligible files unchanged, omit the documented
conflicting artifacts if still present, and repack only affected archives.
Verify the full source inventory against imported and omitted records, all
file and member hashes, retained member bytes, and ZIP CRCs. Refresh provenance
and source documents together; ordinary tests must not rewrite this corpus.
