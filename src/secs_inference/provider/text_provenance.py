"""Distinguish freeform text by producer at disclosure boundaries.

These markers deliberately do not validate strings. The boundary receiving the
text still owns structural limits, and each sink owns disclosure policy.
"""

from typing import NewType


# Caller text may contain private scientific material or hostile presentation
# content. Verified byte identity does not make it publishable.
UserProvidedText = NewType("UserProvidedText", str)

# Model prose may hallucinate or reproduce private prompt material. The marker
# keeps that origin visible in signatures; each eventual sink still decides
# whether the text is appropriate to disclose.
ModelGeneratedText = NewType("ModelGeneratedText", str)

# Provider diagnostics may enter a bounded model-repair conversation. They are
# code-authored facts, not model prose or automatically public product copy.
ProviderDiagnosticText = NewType("ProviderDiagnosticText", str)
