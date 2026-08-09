"""Process-wide outbound TCP/UDP denial for the default local-only runtime."""

from __future__ import annotations

import socket
from ipaddress import ip_address
from threading import Lock
from typing import Any

from cad_harness.domain.errors import HarnessError

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_ORIGINAL_GETHOSTBYADDR = socket.gethostbyaddr
_ORIGINAL_GETNAMEINFO = socket.getnameinfo
_ORIGINAL_SENDTO = socket.socket.sendto
_ORIGINAL_SENDMSG = getattr(socket.socket, "sendmsg", None)
_DEFAULT_SOCKET_TIMEOUT: Any = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT")  # noqa: B009
_INSTALL_LOCK = Lock()
_INSTALLED = False


class OutboundNetworkBlockedError(HarnessError):
    default_required_action = (
        "Keep local-only mode enabled, or explicitly approve and configure an external connector"
    )


def _blocked(address: object) -> OutboundNetworkBlockedError:
    destination_type = "network_endpoint" if isinstance(address, tuple) else type(address).__name__
    return OutboundNetworkBlockedError(
        "Outbound network access is blocked by app.local_only",
        details={"destination_type": destination_type, "local_only": True},
    )


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_host(host: object) -> bool:
    return _is_loopback((host, 0))


def install_local_only_network_guard() -> None:
    """Deny AF_INET/AF_INET6 connections without blocking local Unix/pipe transports."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        def guarded_connect(instance: socket.socket, address: Any) -> None:
            if instance.family in {socket.AF_INET, socket.AF_INET6} and not _is_loopback(address):
                raise _blocked(address)
            _ORIGINAL_CONNECT(instance, address)

        def guarded_connect_ex(instance: socket.socket, address: Any) -> int:
            if instance.family in {socket.AF_INET, socket.AF_INET6} and not _is_loopback(address):
                raise _blocked(address)
            return _ORIGINAL_CONNECT_EX(instance, address)

        def guarded_create_connection(
            address: tuple[str, int],
            timeout: Any = _DEFAULT_SOCKET_TIMEOUT,
            source_address: tuple[str, int] | None = None,
            *,
            all_errors: bool = False,
        ) -> socket.socket:
            if _is_loopback(address):
                return _ORIGINAL_CREATE_CONNECTION(
                    address,
                    timeout=timeout,
                    source_address=source_address,
                    all_errors=all_errors,
                )
            raise _blocked(address)

        def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if not _is_loopback_host(host):
                raise _blocked((host, None))
            return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)

        def guarded_gethostbyname(host: str) -> str:
            if not _is_loopback_host(host):
                raise _blocked((host, None))
            return _ORIGINAL_GETHOSTBYNAME(host)

        def guarded_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
            if not _is_loopback_host(host):
                raise _blocked((host, None))
            return _ORIGINAL_GETHOSTBYNAME_EX(host)

        def guarded_gethostbyaddr(host: str) -> tuple[str, list[str], list[str]]:
            if not _is_loopback_host(host):
                raise _blocked((host, None))
            return _ORIGINAL_GETHOSTBYADDR(host)

        def guarded_getnameinfo(sockaddr: Any, flags: int) -> tuple[str, str]:
            if not _is_loopback(sockaddr):
                raise _blocked(sockaddr)
            return _ORIGINAL_GETNAMEINFO(sockaddr, flags)

        def guarded_sendto(instance: socket.socket, data: Any, *args: Any) -> int:
            address = args[-1] if args else None
            if instance.family in {socket.AF_INET, socket.AF_INET6} and not _is_loopback(address):
                raise _blocked(address)
            return _ORIGINAL_SENDTO(instance, data, *args)

        def guarded_sendmsg(instance: socket.socket, *args: Any, **kwargs: Any) -> int:
            address = kwargs.get("address")
            if address is None and len(args) >= 4:
                address = args[3]
            if instance.family in {socket.AF_INET, socket.AF_INET6} and not _is_loopback(address):
                raise _blocked(address)
            if _ORIGINAL_SENDMSG is None:  # pragma: no cover - installed only where supported
                raise RuntimeError("socket.sendmsg is unavailable")
            return int(_ORIGINAL_SENDMSG(instance, *args, **kwargs))

        setattr(socket.socket, "connect", guarded_connect)  # noqa: B010
        setattr(socket.socket, "connect_ex", guarded_connect_ex)  # noqa: B010
        setattr(socket, "create_connection", guarded_create_connection)  # noqa: B010
        setattr(socket, "getaddrinfo", guarded_getaddrinfo)  # noqa: B010
        setattr(socket, "gethostbyname", guarded_gethostbyname)  # noqa: B010
        setattr(socket, "gethostbyname_ex", guarded_gethostbyname_ex)  # noqa: B010
        setattr(socket, "gethostbyaddr", guarded_gethostbyaddr)  # noqa: B010
        setattr(socket, "getnameinfo", guarded_getnameinfo)  # noqa: B010
        setattr(socket.socket, "sendto", guarded_sendto)  # noqa: B010
        if _ORIGINAL_SENDMSG is not None:
            setattr(socket.socket, "sendmsg", guarded_sendmsg)  # noqa: B010
        _INSTALLED = True


def uninstall_local_only_network_guard() -> None:
    """Restore the socket module for an explicitly external-enabled process."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if not _INSTALLED:
            return
        setattr(socket.socket, "connect", _ORIGINAL_CONNECT)  # noqa: B010
        setattr(socket.socket, "connect_ex", _ORIGINAL_CONNECT_EX)  # noqa: B010
        setattr(socket, "create_connection", _ORIGINAL_CREATE_CONNECTION)  # noqa: B010
        setattr(socket, "getaddrinfo", _ORIGINAL_GETADDRINFO)  # noqa: B010
        setattr(socket, "gethostbyname", _ORIGINAL_GETHOSTBYNAME)  # noqa: B010
        setattr(socket, "gethostbyname_ex", _ORIGINAL_GETHOSTBYNAME_EX)  # noqa: B010
        setattr(socket, "gethostbyaddr", _ORIGINAL_GETHOSTBYADDR)  # noqa: B010
        setattr(socket, "getnameinfo", _ORIGINAL_GETNAMEINFO)  # noqa: B010
        setattr(socket.socket, "sendto", _ORIGINAL_SENDTO)  # noqa: B010
        if _ORIGINAL_SENDMSG is not None:
            setattr(socket.socket, "sendmsg", _ORIGINAL_SENDMSG)  # noqa: B010
        _INSTALLED = False


def configure_local_only_network_guard(*, enabled: bool) -> None:
    """Apply the configured process policy without leaking a previous context's state."""
    if enabled:
        install_local_only_network_guard()
    else:
        uninstall_local_only_network_guard()


def local_only_network_guard_installed() -> bool:
    return _INSTALLED


__all__ = [
    "OutboundNetworkBlockedError",
    "configure_local_only_network_guard",
    "install_local_only_network_guard",
    "local_only_network_guard_installed",
    "uninstall_local_only_network_guard",
]
