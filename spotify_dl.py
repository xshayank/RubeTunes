# -*- coding: utf-8 -*-
"""
Thin compatibility shim for spotify_dl.

All logic lives in the rubetunes/ package.
This file exists so that existing code doing ``import spotify_dl``
and tests doing ``patch("spotify_dl.time")`` continue to work.
"""

# IMPORTANT: ``import time`` must come before the rubetunes star-imports so that
# ``sys.modules['spotify_dl'].time`` is the real time module when
# rubetunes sub-modules call ``_get_time()`` via the sys.modules trick.
import time  # noqa: E402  (must be first)

from rubetunes.logging_setup import *  # noqa: F401,F403
from rubetunes.cache import *  # noqa: F401,F403
from rubetunes.circuit_breaker import *  # noqa: F401,F403
from rubetunes.history import *  # noqa: F401,F403
from rubetunes.tagging import *  # noqa: F401,F403
from rubetunes.spotify_meta import *  # noqa: F401,F403
from rubetunes.providers import *  # noqa: F401,F403
from rubetunes.resolver import *  # noqa: F401,F403
from rubetunes.downloader import *  # noqa: F401,F403
