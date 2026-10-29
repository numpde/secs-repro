"""Owned facts about the model call, independent of inference dependencies."""


class ModelExecutionFailed(RuntimeError):
    """The named model forward or device completion raised."""

    def __init__(self, modality, stage):
        if modality not in ("h_nmr", "smiles") or stage not in ("forward", "completion"):
            raise ValueError("Model execution failure requires a known modality and stage")
        self.modality, self.stage = modality, stage
        super().__init__(f"The {modality} model {stage} did not return a result")
