#!/usr/bin/env python3
"""Race-resistant path handling for private Degen Dogs runner artifacts.

Every path component is opened relative to an already-validated directory
descriptor.  Symlinks are rejected at every level, except for the small set of
root-owned, immutable aliases that macOS creates at the filesystem root.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
from collections.abc import Sequence


class SecurePathError(RuntimeError):
    """Raised when a runner path cannot be traversed without following links."""


_ALLOWED_ROOT_ALIASES: dict[str, tuple[str, ...]] = {
    "etc": ("private", "etc"),
    "tmp": ("private", "tmp"),
    "var": ("private", "var"),
}
_CURRENT_UID = os.getuid()
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _require_platform_support() -> None:
    if not _NOFOLLOW or not _DIRECTORY:
        raise SecurePathError("this platform lacks O_NOFOLLOW or O_DIRECTORY")
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise SecurePathError("this platform lacks descriptor-relative path operations")


def _absolute_parts(path: os.PathLike[str] | str) -> tuple[str, tuple[str, ...]]:
    raw = os.path.expanduser(os.fsdecode(os.fspath(path)))
    if not raw or not os.path.isabs(raw):
        raise SecurePathError(f"path must be absolute: {raw or '<empty>'}")
    pieces: list[str] = []
    for piece in raw.split(os.sep):
        if not piece or piece == ".":
            continue
        if piece == "..":
            raise SecurePathError(f"parent traversal is not allowed: {raw}")
        if os.sep in piece or (os.altsep and os.altsep in piece):
            raise SecurePathError(f"invalid path component in: {raw}")
        pieces.append(piece)
    normalized = os.sep + os.sep.join(pieces)
    return normalized, tuple(pieces)


def _directory_flags() -> int:
    return os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC


def _rewrite_allowed_root_alias(
    root_fd: int, parts: tuple[str, ...], display_path: str
) -> tuple[str, ...]:
    if not parts or parts[0] not in _ALLOWED_ROOT_ALIASES:
        return parts
    alias = parts[0]
    try:
        details = os.stat(alias, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise SecurePathError(f"cannot inspect root path component {alias}: {exc}") from exc
    if not stat.S_ISLNK(details.st_mode):
        return parts

    root_details = os.fstat(root_fd)
    expected = _ALLOWED_ROOT_ALIASES[alias]
    try:
        target = os.readlink(alias, dir_fd=root_fd)
    except OSError as exc:
        raise SecurePathError(f"cannot inspect root alias /{alias}: {exc}") from exc
    target_parts = tuple(piece for piece in target.lstrip(os.sep).split(os.sep) if piece)
    if (
        details.st_uid != 0
        or root_details.st_uid != 0
        or stat.S_IMODE(root_details.st_mode) & 0o022
        or target_parts != expected
    ):
        raise SecurePathError(f"root alias is not an immutable system alias: {display_path}")
    return expected + parts[1:]


def _validate_directory(details: os.stat_result, display_path: str, *, final: bool) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise SecurePathError(f"path component is not a directory: {display_path}")
    if details.st_uid not in {0, _CURRENT_UID}:
        raise SecurePathError(f"directory is owned by an unexpected user: {display_path}")
    # Root-owned shared roots such as /tmp are traversed only through pinned
    # descriptors.  A user-owned writable ancestor would let a second account
    # replace later components, so only the final directory may be hardened.
    if details.st_uid == _CURRENT_UID and not final and stat.S_IMODE(details.st_mode) & 0o022:
        raise SecurePathError(f"directory ancestor is writable by another user: {display_path}")


def _open_directory_parts(
    parts: tuple[str, ...],
    display_path: str,
    *,
    create: bool,
    private_final: bool,
    private_change_out: list[bool] | None = None,
) -> int:
    _require_platform_support()
    current_fd = os.open(os.sep, _directory_flags())
    try:
        parts = _rewrite_allowed_root_alias(current_fd, parts, display_path)
        if not parts:
            details = os.fstat(current_fd)
            if private_final and details.st_uid != _CURRENT_UID:
                raise SecurePathError("filesystem root cannot be used as a private directory")
            return current_fd

        traversed: list[str] = []
        for index, component in enumerate(parts):
            traversed.append(component)
            component_path = os.sep + os.sep.join(traversed)
            is_final = index == len(parts) - 1
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    # A concurrent creator won. Opening with O_NOFOLLOW below
                    # validates what appeared instead of trusting it.
                    pass
                try:
                    next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
                except OSError as exc:
                    raise SecurePathError(
                        f"refusing newly appeared directory component {component_path}: {exc}"
                    ) from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SecurePathError(
                        f"symlink or non-directory ancestor is not allowed: {component_path}"
                    ) from exc
                raise SecurePathError(f"cannot open directory component {component_path}: {exc}") from exc

            try:
                details = os.fstat(next_fd)
                _validate_directory(
                    details,
                    component_path,
                    final=is_final and private_final,
                )
                if is_final and private_final:
                    if details.st_uid != _CURRENT_UID:
                        raise SecurePathError(
                            f"private directory is not owned by the current user: {display_path}"
                        )
                    changed = stat.S_IMODE(details.st_mode) != 0o700
                    if changed:
                        os.fchmod(next_fd, 0o700)
                    if private_change_out is not None:
                        private_change_out.append(changed)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def open_secure_directory(
    path: os.PathLike[str] | str, *, create: bool = False, private: bool = False
) -> int:
    """Return an fd for *path*, rejecting symlinks in every ancestor."""

    display_path, parts = _absolute_parts(path)
    return _open_directory_parts(
        parts,
        display_path,
        create=create,
        private_final=private,
    )


def _open_parent(
    path: os.PathLike[str] | str,
    *,
    create: bool,
    private_parent: bool,
) -> tuple[int, str, str]:
    display_path, parts = _absolute_parts(path)
    if not parts:
        raise SecurePathError("filesystem root cannot be used as a file")
    parent_parts = parts[:-1]
    parent_display = os.path.dirname(display_path) or os.sep
    parent_fd = _open_directory_parts(
        parent_parts,
        parent_display,
        create=create,
        private_final=private_parent,
    )
    return parent_fd, parts[-1], display_path


def _validate_regular(
    descriptor: int,
    display_path: str,
    *,
    require_private_mode: bool,
) -> os.stat_result:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise SecurePathError(f"path is not a regular file: {display_path}")
    if details.st_uid != _CURRENT_UID:
        raise SecurePathError(f"file is not owned by the current user: {display_path}")
    if details.st_nlink != 1:
        raise SecurePathError(f"hard-linked private file is not allowed: {display_path}")
    if require_private_mode and stat.S_IMODE(details.st_mode) & 0o077:
        raise SecurePathError(f"private file permissions are too broad: {display_path}")
    return details


def ensure_private_directory(path: os.PathLike[str] | str) -> bool:
    """Create/harden a private directory and report whether its mode changed."""

    display_path, parts = _absolute_parts(path)
    changes: list[bool] = []
    descriptor = _open_directory_parts(
        parts,
        display_path,
        create=True,
        private_final=True,
        private_change_out=changes,
    )
    os.close(descriptor)
    return changes[0] if changes else False


def ensure_private_file(path: os.PathLike[str] | str, *, create: bool = True) -> bool:
    """Validate/harden an owned file and report whether its mode changed."""

    try:
        parent_fd, name, display_path = _open_parent(
            path,
            create=False,
            private_parent=False,
        )
    except FileNotFoundError:
        if not create:
            return False
        raise SecurePathError(f"private file parent does not exist: {path}") from None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            if not create:
                return False
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                    dir_fd=parent_fd,
                )
        except OSError as exc:
            raise SecurePathError(f"refusing unsafe private file {display_path}: {exc}") from exc
        try:
            _validate_regular(descriptor, display_path, require_private_mode=False)
            changed = stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600
            if changed:
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return changed
    finally:
        os.close(parent_fd)


def open_private_lock(path: os.PathLike[str] | str) -> int:
    """Create/open a private lock through a private, descriptor-pinned parent."""

    parent_fd, name, display_path = _open_parent(
        path,
        create=True,
        private_parent=True,
    )
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise SecurePathError(f"refusing unsafe private lock {display_path}: {exc}") from exc
        try:
            _validate_regular(descriptor, display_path, require_private_mode=False)
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_fd)


def open_existing_private_file(path: os.PathLike[str] | str, *, writable: bool = False) -> int:
    parent_fd, name, display_path = _open_parent(
        path,
        create=False,
        private_parent=False,
    )
    try:
        flags = os.O_RDWR if writable else os.O_RDONLY
        descriptor = os.open(
            name,
            flags | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=parent_fd,
        )
        try:
            _validate_regular(descriptor, display_path, require_private_mode=True)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_fd)


def inspect_existing_private_file(
    path: os.PathLike[str] | str,
    *,
    require_private_mode: bool = True,
) -> os.stat_result | None:
    """Descriptor-safely inspect an existing owned, single-link regular file.

    ``None`` means the lexical path is absent. Unsafe ancestors, symlinks,
    non-regular files, unexpected ownership, and hard links raise
    :class:`SecurePathError`. Callers that only need to diagnose permission
    drift may disable the mode check without weakening the other guarantees.
    """

    try:
        parent_fd, name, display_path = _open_parent(
            path,
            create=False,
            private_parent=False,
        )
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecurePathError(f"refusing unsafe private file {display_path}: {exc}") from exc
        try:
            return _validate_regular(
                descriptor,
                display_path,
                require_private_mode=require_private_mode,
            )
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def create_private_temp(prefix: os.PathLike[str] | str) -> str:
    """Create a mode-0600 sibling using *prefix* and return its lexical path."""

    parent_fd, prefix_name, display_prefix = _open_parent(
        prefix,
        create=False,
        private_parent=True,
    )
    try:
        for _ in range(128):
            suffix = secrets.token_hex(8)
            name = f"{prefix_name}.{suffix}"
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            try:
                _validate_regular(descriptor, name, require_private_mode=False)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            return f"{display_prefix}.{suffix}"
    finally:
        os.close(parent_fd)
    raise SecurePathError(f"could not allocate private temporary file: {display_prefix}")


def unlink_private_file(path: os.PathLike[str] | str, *, missing_ok: bool = False) -> bool:
    try:
        parent_fd, name, display_path = _open_parent(
            path,
            create=False,
            private_parent=False,
        )
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        try:
            _validate_regular(descriptor, display_path, require_private_mode=True)
        finally:
            os.close(descriptor)
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def replace_private_file(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
    """Atomically replace *target* with a protected owned regular *source*."""

    source_parent, source_name, source_display = _open_parent(
        source,
        create=False,
        private_parent=False,
    )
    try:
        target_parent, target_name, target_display = _open_parent(
            target,
            create=False,
            private_parent=False,
        )
        try:
            source_fd = os.open(
                source_name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=source_parent,
            )
            try:
                source_details = _validate_regular(
                    source_fd,
                    source_display,
                    require_private_mode=True,
                )
                source_identity = (source_details.st_dev, source_details.st_ino)
            finally:
                os.close(source_fd)

            try:
                target_fd = os.open(
                    target_name,
                    os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                    dir_fd=target_parent,
                )
            except FileNotFoundError:
                target_fd = None
            if target_fd is not None:
                try:
                    _validate_regular(target_fd, target_display, require_private_mode=True)
                finally:
                    os.close(target_fd)

            os.replace(
                source_name,
                target_name,
                src_dir_fd=source_parent,
                dst_dir_fd=target_parent,
            )
            installed_fd = os.open(
                target_name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                dir_fd=target_parent,
            )
            try:
                installed_details = _validate_regular(
                    installed_fd,
                    target_display,
                    require_private_mode=False,
                )
                if (installed_details.st_dev, installed_details.st_ino) != source_identity:
                    raise SecurePathError(
                        f"installed file identity changed during replacement: {target_display}"
                    )
                os.fchmod(installed_fd, 0o600)
                os.fsync(installed_fd)
            finally:
                os.close(installed_fd)
            os.fsync(target_parent)
            source_parent_details = os.fstat(source_parent)
            target_parent_details = os.fstat(target_parent)
            if (source_parent_details.st_dev, source_parent_details.st_ino) != (
                target_parent_details.st_dev,
                target_parent_details.st_ino,
            ):
                os.fsync(source_parent)
        finally:
            os.close(target_parent)
    finally:
        os.close(source_parent)


def _usage() -> str:
    return (
        "usage: runner_path_security.py "
        "{private-dir|private-file|private-lock-check|private-temp|private-unlink} PATH [CREATE]"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    operation, path, *rest = args
    try:
        if operation == "private-dir" and not rest:
            ensure_private_directory(path)
        elif operation == "private-file" and len(rest) == 1 and rest[0] in {"0", "1"}:
            ensure_private_file(path, create=rest[0] == "1")
        elif operation == "private-lock-check" and not rest:
            descriptor = open_private_lock(path)
            os.close(descriptor)
        elif operation == "private-temp" and not rest:
            print(create_private_temp(path))
        elif operation == "private-unlink" and not rest:
            unlink_private_file(path, missing_ok=True)
        else:
            print(_usage(), file=sys.stderr)
            return 2
    except (OSError, SecurePathError) as exc:
        print(f"error: refusing unsafe runner path: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
