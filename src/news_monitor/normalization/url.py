"""Deterministic URL normalization used for duplicate detection."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "mtm_", "_hs", "vero_")
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "yclid",
        "ysclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "referrer",
        "source",
        "amp",
        "from",
    }
)
DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    if lowered in TRACKING_PARAMS:
        return True
    return any(lowered.startswith(prefix) for prefix in TRACKING_PREFIXES)


def normalize_url(url: str, base_url: str | None = None) -> str:
    """Return a canonical form of ``url``.

    The transformation is deterministic and idempotent:

    * relative links are resolved against ``base_url``;
    * scheme and host are lower-cased, ``www.`` and default ports are dropped;
    * the fragment and tracking query parameters are removed;
    * remaining query parameters are sorted;
    * a trailing slash is removed from non-root paths.
    """
    if url is None:
        raise ValueError("url must not be None")
    candidate = url.strip()
    if not candidate:
        raise ValueError("url must not be empty")
    if base_url:
        candidate = urljoin(str(base_url), candidate)

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower() or "https"
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported url scheme: {parts.scheme!r}")

    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"url without host: {url!r}")
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    port = parts.port
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query = urlencode(sorted(query_pairs), doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def same_host(url: str, base_url: str) -> bool:
    """Return ``True`` when both urls share a registrable host (``www.`` ignored)."""
    try:
        left = urlsplit(normalize_url(url)).hostname or ""
        right = urlsplit(normalize_url(str(base_url))).hostname or ""
    except ValueError:
        return False
    return bool(left) and left == right
