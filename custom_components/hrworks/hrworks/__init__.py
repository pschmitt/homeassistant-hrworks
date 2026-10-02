"""HR WORKS employee library. The browser worker remains a separate process."""

from .auth import authenticate, complete_mfa, secret_settings
from .client import WorkerClient
from .totp import InvalidTotp, totp_code
from .transport import LocalTransport, SshTransport, WorkerError

__version__ = "2.0.0"
__all__ = [
    "WorkerClient",
    "WorkerError",
    "LocalTransport",
    "SshTransport",
    "InvalidTotp",
    "authenticate",
    "complete_mfa",
    "secret_settings",
    "totp_code",
]
