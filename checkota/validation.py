"""Shared untrusted-input predicates and Google-URL validation.

The check-in response and the OTA redirect chain are both server-controlled.
URL acceptance rules live here so the previously duplicated control-char scans
and HTTPS/host/path checks cannot drift apart on this security-critical path.
"""

from urllib.parse import urlsplit


def has_control_chars(value: str) -> bool:
    """True if value contains C0, DEL, or C1 control characters.

    C1 controls (U+0080..U+009F) are included because terminal emulators may
    interpret them as escape/control sequences even though they are not ASCII.
    """
    return any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value)


def has_unsafe_url_chars(value: str) -> bool:
    """True if value contains control characters or any whitespace.

    URLs are rejected outright on whitespace (including newlines): a hostile
    response could otherwise forge log lines or split requests.
    """
    return any(
        char.isspace() or ord(char) < 32 or 0x7F <= ord(char) <= 0x9F
        for char in value
    )


def _host_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    for host in allowed_hosts:
        if host.startswith("."):
            # Suffix entry: ".gvt1.com" accepts gvt1.com itself and any
            # subdomain -- Google owns that whole zone.
            if hostname == host[1:] or hostname.endswith(host):
                return True
        elif hostname == host:
            return True
    return False


def is_google_https_url(
    value: str,
    *,
    allowed_hosts: tuple[str, ...],
    path_prefixes: tuple[str, ...],
) -> bool:
    """Accept only HTTPS URLs on exactly-allowlisted Google hosts.

    Rejects whitespace/control characters, non-HTTPS schemes, explicit ports,
    userinfo, hosts outside the allowlist (exact entries must match exactly;
    entries starting with "." match the zone and its subdomains), and paths
    outside the given prefixes. urlsplit lowercases scheme and hostname, so
    case tricks fail; invalid ports raise ValueError and are caught.
    """
    try:
        parsed = urlsplit(value)
        return (
            not has_unsafe_url_chars(value)
            and parsed.scheme == "https"
            and _host_allowed(parsed.hostname or "", allowed_hosts)
            and parsed.port is None
            and not parsed.username
            and not parsed.password
            and parsed.path.startswith(path_prefixes)
        )
    except ValueError:
        return False
