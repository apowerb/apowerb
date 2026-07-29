class InvalidCredentials(Exception):
    pass


class EmailNotVerified(Exception):
    """Raised at login when the account email is not verified yet."""
    pass
