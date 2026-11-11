"""Comparison failures shared by local execution, transport and workflows."""


class PDFComparisonError(ValueError):
    pass


class StaleUploadComparison(PDFComparisonError):
    pass


class ComparisonInProgress(PDFComparisonError):
    pass


class LocalComparisonConfirmationRequired(PDFComparisonError):
    """No new local computation may start without a bound, explicit choice."""

    def __init__(self, message, *, can_confirm=True, existing_local=False):
        super().__init__(message)
        self.can_confirm = can_confirm
        self.existing_local = existing_local
        self.confirmation_token = ""
