# Reference decoder spike

This test-only probe evaluates whether the pinned frontend decoder can support
the intended enumerate-then-select boundary for JCAMP-DX. The probe owns a
candidate projection from the decoder's entry tree into representations; that
projection is project code, not an upstream inventory API.

The tests establish only the cases they name: processed one-dimensional
spectra, grouped complex channels, grouped two-dimensional rows, and linked
spectrum and peak-table entries. The tests supply the same fixture bytes to
inventory and preparation, then compare the selected one-dimensional data
through the pinned frontend normalization. The IDs are probe-local positional
locators: they do not bind source bytes or establish stable identity. The spike
also does not establish the final Python SECS preparation vector, production
containment, other format families, or permission to distribute or operate the
decoder.
