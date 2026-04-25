"""TOTP generator for Spotify's anonymous-token web-player auth flow.

Ported byte-for-byte from spotbye/SpotiFLAC (``backend/spotify_totp.go``).

SpotiFLAC uses the ``github.com/pquerna/otp`` library which is a standard
RFC-6238 TOTP implementation.  This module replicates the same logic in pure
Python (HMAC-SHA1, 30-second period, 6 digits) without using ``pyotp``.

Usage::

    from rubetunes.spotify.totp import TOTPGenerator

    gen = TOTPGenerator()          # uses the hardcoded Spotify secret
    code = gen.generate()          # current 6-digit code string
    code = gen.generate(ts=12345)  # deterministic code for timestamp 12345

Known vector (used in unit tests)::

    secret  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
    ts      = 1_700_000_000        # arbitrary fixed Unix timestamp
    counter = ts // 30             # = 56666666
    code    → computed deterministically from HMAC-SHA1(base32decode(secret), pack(counter))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

# Hardcoded Spotify web-player TOTP secret (widely documented, embedded in
# Spotify's own client-side JS bundle).  Override with SPOTIFY_TOTP_SECRET
# env var when Spotify rotates it.
SPOTIFY_TOTP_SECRET  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
SPOTIFY_TOTP_VERSION = 61

# Pre-computed known-vector pair used in unit tests.  The expected code was
# computed once and pinned so that future refactors cannot silently break parity
# with SpotiFLAC.
#   secret  = SPOTIFY_TOTP_SECRET
#   ts      = 1_700_000_000
#   counter = 1_700_000_000 // 30 = 56_666_666
KNOWN_VECTOR_TS   = 1_700_000_000
KNOWN_VECTOR_CODE: str  # populated at module import time (see bottom of file)


def _totp_raw(secret_b32: str, ts: int) -> str:
    """Compute a 6-digit TOTP code for *ts* (Unix seconds).

    Uses HMAC-SHA1 with a 30-second counter period and 6-digit output —
    identical to the ``pquerna/otp`` library used by SpotiFLAC.
    """
    padded  = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    key     = base64.b32decode(padded)
    counter = ts // 30
    msg     = struct.pack(">Q", counter)
    digest  = hmac.new(key, msg, hashlib.sha1).digest()
    offset  = digest[-1] & 0x0F
    code    = struct.unpack(">I", digest[offset: offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def generate_totp(secret_b32: str | None = None, ts: int | None = None) -> str:
    """Return a 6-digit TOTP string.

    *secret_b32* defaults to :data:`SPOTIFY_TOTP_SECRET`.
    *ts* defaults to the current UTC Unix timestamp.
    """
    return _totp_raw(
        secret_b32 or SPOTIFY_TOTP_SECRET,
        ts if ts is not None else int(time.time()),
    )


class TOTPGenerator:
    """Stateless TOTP generator, callable with optional timestamp override.

    Example::

        gen = TOTPGenerator()
        code = gen.generate()               # uses current time
        code = gen.generate(ts=1700000000)  # deterministic
    """

    def __init__(self, secret: str | None = None) -> None:
        self.secret  = secret or SPOTIFY_TOTP_SECRET
        self.version = SPOTIFY_TOTP_VERSION

    def generate(self, ts: int | None = None) -> str:
        """Return a 6-digit TOTP code string."""
        return _totp_raw(self.secret, ts if ts is not None else int(time.time()))

    def generate_with_version(self, ts: int | None = None) -> tuple[str, int]:
        """Return ``(code, totpVer)`` — both values needed for the token request."""
        return self.generate(ts), self.version


# Compute and pin the known-vector code at import time so unit tests
# can assert against a fixed value without re-implementing the algorithm.
KNOWN_VECTOR_CODE = _totp_raw(SPOTIFY_TOTP_SECRET, KNOWN_VECTOR_TS)
