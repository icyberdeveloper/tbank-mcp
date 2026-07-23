"""T-Bank (Т-Банк) mobile-banking MCP server.

Self-bootstrapping: login(phone) -> confirm_otp(otp) does a real SSO login
(captures the long-lived SSO_SESSION + session). After that the MCP runs fully
headless: silent_relogin() re-mints the session (SSO_SESSION + a built-in device
fingerprint, no OTP) ~every 2h, and reads + the messenger tmsg work from the
resulting session. Read endpoint shapes are built into `endpoints.py` (static
API params only — no device/session/account secrets).

`pay`/`group_pay` use HMAC-SHA256 `x-api-signature` (key = sessionid); reads are
cookie+Bearer (no signature); TLS self-heals on cert rotation.
"""
from .client import (
    MobileSession, TbankApiError, SessionExpired, ms_for_period, MOBILE_BASE,
)

__all__ = [
    "MobileSession", "TbankApiError", "SessionExpired",
    "ms_for_period", "MOBILE_BASE",
]
