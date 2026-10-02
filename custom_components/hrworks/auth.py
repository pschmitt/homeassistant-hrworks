"""Home Assistant imports the bundled shared authentication implementation."""

from .hrworks.auth import authenticate, complete_mfa, secret_settings

__all__ = ["authenticate", "complete_mfa", "secret_settings"]
