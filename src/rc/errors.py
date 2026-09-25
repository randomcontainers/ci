class RcError(Exception):
    """An error whose message is meant for the person running rc."""


class ValidationError(RcError):
    def __init__(self, origin: str, problems: list[str]):
        self.origin = origin
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{origin} is invalid:\n{lines}")
