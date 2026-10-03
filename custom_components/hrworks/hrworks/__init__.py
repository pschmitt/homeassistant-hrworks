"""HR WORKS employee library. The browser worker remains a separate process."""

from importlib.metadata import PackageNotFoundError, version

from .auth import authenticate, complete_mfa, secret_settings
from .client import WorkerClient
from .totp import InvalidTotp, totp_code
from .transport import LocalTransport, SshTransport, WorkerError

try:
    __version__ = version("hrworks-employee")
except PackageNotFoundError:
    # Home Assistant bundles the library without installing the CLI distribution.
    __version__ = "uninstalled"
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
