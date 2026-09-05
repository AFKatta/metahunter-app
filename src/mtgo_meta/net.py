"""HTTPS that does not depend on the machine's certificate store.

Every outbound call in this app goes through here, and the reason is a
bug that cost an afternoon: on Windows, Python validates TLS against
the Windows root store, which the OS fills in lazily — Schannel fetches
a root the first time it needs one, and Python's snapshot of the store
never triggers that. On a machine whose store is missing the current
Let's Encrypt chain, or still holds an expired cross-signed path, every
HTTPS call to our own API fails with:

    CERTIFICATE_VERIFY_FAILED: certificate has expired

while ``curl`` on the same machine succeeds, because it carries its own
roots. The server's certificate is perfectly valid; the client simply
cannot see a path to a trusted root.

That failure mode is unacceptable here. Uploads would stop silently,
and sign-in — which has to reach the server at least once — would be
impossible with no explanation the user could act on. So we carry our
own root bundle via ``certifi``, exactly as ``requests`` does, and stop
depending on the state of somebody's Windows installation.

If ``certifi`` is somehow absent we fall back to the system defaults
rather than refusing to talk to anything: degraded is better than dead,
and on Linux and macOS the system store is usually fine.
"""

from __future__ import annotations

import logging
import ssl
import urllib.request
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that carries its own trust roots.

    Cached: building a context parses the whole CA bundle, which is
    wasted work on every request.
    """
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        log.debug("TLS: using certifi bundle at %s", certifi.where())
        return ctx
    except Exception as exc:  # noqa: BLE001 - missing, unreadable, anything
        log.warning(
            "TLS: certifi unavailable (%s); falling back to the system "
            "certificate store, which may reject valid certificates on "
            "Windows", exc,
        )
        return ssl.create_default_context()


def urlopen(req: Any, timeout: int = DEFAULT_TIMEOUT):
    """``urllib.request.urlopen`` with our own trust roots.

    Takes a Request or a URL string, like the original. Verification
    stays on — the fix is to supply the right roots, never to skip the
    check.
    """
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
