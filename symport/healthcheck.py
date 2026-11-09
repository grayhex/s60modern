"""Container health probe: the unauthenticated /healthz answers over HTTP or, in Home LAN mode, HTTPS."""

from __future__ import annotations

import os
import ssl
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("SYMPORT_HTTP_PORT", "8765")
    insecure = ssl.create_default_context()
    insecure.check_hostname = False  # loopback probe of our own server; the certificate names the LAN address
    insecure.verify_mode = ssl.CERT_NONE
    for scheme in ("http", "https"):
        try:
            with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=3, context=insecure) as reply:
                if reply.status == 200:
                    return 0
        except (OSError, ValueError):
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
