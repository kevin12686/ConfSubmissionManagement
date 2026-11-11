"""Comparison failures shared by local execution, transport and workflows."""


class PDFComparisonError(ValueError):
    pass


class StaleUploadComparison(PDFComparisonError):
    pass


class ComparisonInProgress(PDFComparisonError):
    pass
