def get_domain_from_email(email: str) -> str:
    """Extract domain name from email address."""
    if "@" not in email:
        raise ValueError("Invalid email format")

    domain = email.split("@")[1]
    return domain


def get_domain_from_email_or_none(email: str | None) -> str | None:
    """Like get_domain_from_email but returns None for missing/malformed
    addresses instead of raising. Use for best-effort org checks where a
    bad value must not crash the request."""
    if not email or "@" not in email:
        return None
    return email.split("@")[1]
