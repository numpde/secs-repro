"""Scientific outcome vocabulary and the retrieval-empty explanation."""

from enum import StrEnum


class AnalysisOutcome(StrEnum):
    ANALYSED = "analysed"
    CANNOT_ANALYSE = "cannot_analyse"
    NO_STARTING_CANDIDATES = "no_starting_candidates"


NO_STARTING_CANDIDATES_MESSAGE = (
    "No elucidation was produced. Candidate retrieval returned no starting molecules under the configured search. "
    "Graph GA was not run. This does not establish that the formula is invalid "
    "or that no matching structure exists."
)
