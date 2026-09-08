"""Bind explicit input selections to the existing scientific readers."""

from secs_inference.provider.input_operations import BrukerSelection, JcampSelection, Selection
from secs_inference.provider.source_access import SourceAccess, bruker_sources
from secs_inference.spectra.bruker import read_bruker_pdata
from secs_inference.spectra.jcamp import read_jcamp_spectrum
from secs_inference.spectra.secs import prepare_secs_spectrum


def prepare_selected_spectrum(access: SourceAccess, selection: Selection):
    """Decode only the chosen representation, then use SECS-owned preparation."""
    if isinstance(selection, JcampSelection):
        with access.materialize({"spectrum.jdx": selection.source}) as root:
            spectrum = read_jcamp_spectrum(root / "spectrum.jdx")
    elif isinstance(selection, BrukerSelection):
        sources = bruker_sources(selection.upload_ref, selection.pdata_directory)
        with access.materialize(sources) as root:
            spectrum = read_bruker_pdata(root)
    else:
        raise AssertionError("No reader is bound to the selected input operation")
    return prepare_secs_spectrum(spectrum)
