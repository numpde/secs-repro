"""Selection and acquisition facts explicitly attached by the analysis owner."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisContext:
    """Admit owned run evidence, not a same-named attribute on a library error."""

    interpretation_rejections: list[dict]
    input_choices: list[dict]
    acquired_uploads: dict
