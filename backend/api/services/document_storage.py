from __future__ import annotations

import sys

from core.documents import storage as _storage


sys.modules[__name__] = _storage
