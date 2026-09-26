"""Attach stable support evidence IDs to the tests that prove them."""

_EVIDENCE_ATTRIBUTE = "__support_evidence__"


def qualification_evidence(evidence_id: str):
    """Attach one evidence ID without making a test name contractual."""

    def decorate(test):
        if hasattr(test, _EVIDENCE_ATTRIBUTE):
            raise ValueError("Qualification evidence was already attached to this test")
        setattr(test, _EVIDENCE_ATTRIBUTE, (evidence_id,))
        return test

    return decorate


def test_evidence(test) -> tuple[str, ...]:
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    return getattr(method, _EVIDENCE_ATTRIBUTE, ())
