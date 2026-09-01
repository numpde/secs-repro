# Published SECS challenges

These twenty spectra and their expected structures come from the challenge
dataset published with the SECS paper. The spectrum JSON files are copied
byte-for-byte from `public/challenges/` in the project frontend; each contains
the normalized 10,000-point vector consumed by SECS.

`cases.json` keeps the input formula and expected structure, the paper's rank,
and the rank produced by this repository's full-index ScoreOnly baseline. The
two ranks describe different searches and are deliberately not equated. A null
rank means that search did not find the expected structure. The recorded
candidate-manifest digest binds the ScoreOnly ranks to the exact full index
that produced them.

Source: `numpde/fork-of-elucidation.cheminfo.org` at
`5ab78f61e9fb679f3f0b9823be5217ae250e213f`.
