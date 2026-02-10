class DomainError(Exception):
    """Base domain error for user-facing business failures."""


class AlreadyExists(DomainError):
    pass


class NotFound(DomainError):
    pass


class PermissionDenied(DomainError):
    pass


class InvalidInput(DomainError):
    pass


class BusinessRuleViolation(DomainError):
    pass
