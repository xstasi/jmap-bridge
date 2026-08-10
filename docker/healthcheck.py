"""Container healthcheck for the production image (see ../Containerfile).

Every real bridge route requires HTTP Basic auth (401) or issues a redirect
(301), so a plain 2xx-or-bust check always "fails" against a perfectly
healthy server. Any HTTP response at all - even 401/301 - means the server
is up and answering; only a connection-level failure (refused, timeout, no
response) means unhealthy. So urllib.error.HTTPError counts as healthy;
URLError (and anything else) does not.
"""

import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("PORT", "8080")
url = f"http://127.0.0.1:{port}/.well-known/jmap"

try:
    urllib.request.urlopen(url, timeout=3)
except urllib.error.HTTPError:
    pass
except Exception:
    sys.exit(1)
