"""Access-control layer for the Kharej VPS worker.

This module will enforce the same whitelist / ban-list semantics that the
existing Rubika bot (``rub.py``) uses, but driven by the shared state
propagated from the Iran VPS admin panel via Rubika control messages:
- ``whitelist.update`` — replace the allowed-user list.
- ``ban.update`` — add/remove banned users.
- ``access.check(user_id)`` — gate function called by the dispatcher.

State is persisted locally in ``kharej/state/`` so that the worker can
enforce access rules even during a brief Rubika connectivity blip.

# TODO(step-5): implement
"""

from __future__ import annotations
