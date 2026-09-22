import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    pass


def _host_is_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    return not allowed_hosts or any(
        hostname == allowed or hostname.endswith(f".{allowed}") for allowed in allowed_hosts
    )


def validate_public_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("Only absolute HTTP or HTTPS product URLs are accepted")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or not _host_is_allowed(hostname, allowed_hosts):
        raise UnsafeUrlError("The product URL host is not allowed")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise UnsafeUrlError("The product URL host could not be resolved") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeUrlError("Private, local, and reserved network targets are not allowed")

    return url

