"""Optional outbound HTTP proxy configuration.

Set WEBHARE_PROXY in the runtime environment as a full proxy URL, e.g.:
  http://username:password@host:port

The value is intentionally not hard-coded so proxy credentials are not stored
in source control.
"""

import os
from typing import Optional


def get_proxy_url() -> Optional[str]:
    """Return the configured HTTP proxy URL, or None when proxying is disabled."""
    value = os.getenv("WEBHARE_PROXY", "").strip()
    return value or None
