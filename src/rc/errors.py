class RcError(Exception):
    """An error whose message is meant for the person running rc."""


class TransientError(RcError):
    """A timeout, a failed connection, or an HTTP 429 or 5xx answer: likely to pass on its own."""


def http_error(what: str, status: int) -> RcError:
    cls = TransientError if status == 429 or status >= 500 else RcError
    return cls(f"{what}: HTTP {status}")


class ValidationError(RcError):
    def __init__(self, origin: str, problems: list[str]):
        self.origin = origin
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{origin} is invalid:\n{lines}")
