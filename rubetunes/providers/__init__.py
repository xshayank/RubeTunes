from __future__ import annotations

"""Providers sub-package — re-exports all provider symbols."""

from rubetunes.providers.deezer import *   # noqa: F401, F403
from rubetunes.providers.qobuz import *    # noqa: F401, F403
from rubetunes.providers.tidal import *    # noqa: F401, F403
from rubetunes.providers.tidal_alt import *  # noqa: F401, F403
from rubetunes.providers.amazon import *   # noqa: F401, F403
from rubetunes.providers.youtube import *  # noqa: F401, F403

# Explicit __all__ needed so that `from rubetunes.providers import *`
# re-exports private (_-prefixed) names into caller's namespace.
from rubetunes.providers import deezer as _dz, qobuz as _qz, tidal as _td, tidal_alt as _ta, amazon as _am, youtube as _yt  # noqa: E501

__all__: list[str] = []
for _m in [_dz, _qz, _td, _ta, _am, _yt]:
    __all__.extend(getattr(_m, "__all__", []))
del _dz, _qz, _td, _ta, _am, _yt, _m
