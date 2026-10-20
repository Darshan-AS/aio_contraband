"""Immutable domain error types for explicit error propagation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContrabandError:
    """Base immutable domain error."""

    message: str

    def __str__(self) -> str:
        return self.message


type StreamWeaverError = ContrabandError


@dataclass(frozen=True, slots=True)
class SecretError(ContrabandError):
    """Raised when required secrets are missing during config injection."""

    missing_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CryptoError(ContrabandError):
    """Raised when encryption or decryption of AIOManager envelopes fails."""


@dataclass(frozen=True, slots=True)
class ConfigValidationError(ContrabandError):
    """Raised when a service configuration payload has an invalid structure."""

    service: str = ""


@dataclass(frozen=True, slots=True)
class BencodeError(ContrabandError):
    """Raised when decoding bencoded `.torrent` bytes fails."""


@dataclass(frozen=True, slots=True)
class ParseError(ContrabandError):
    """Raised when parsing a magnet URI, course HTML, or catalog file fails."""


@dataclass(frozen=True, slots=True)
class NetworkError(ContrabandError):
    """Raised when an HTTP request fails or returns an unexpected status."""

    status_code: int = 0
    url: str = ""


@dataclass(frozen=True, slots=True)
class StorageError(ContrabandError):
    """Raised when a filesystem operation or path boundary check fails."""

    path: str = ""


@dataclass(frozen=True, slots=True)
class GitError(ContrabandError):
    """Raised when a Git repository operation fails."""
