"""Local permission-controlled plaintext files. This is NOT encryption."""

import os
from pathlib import Path
import re
import stat
import tempfile

from job_search_assistant.core.errors import InfrastructureError


class FileSecretStore:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise OSError("Unsafe directory")
            self.directory.chmod(0o700)
        except OSError:
            raise InfrastructureError("Credential storage is unavailable.") from None

    def _path(self, identifier):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
            raise InfrastructureError("Invalid credential identifier.")
        if self.directory.is_symlink():
            raise InfrastructureError("Credential storage is unavailable.")
        return self.directory / identifier

    def get(self, identifier):
        path = self._path(identifier)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError("Unsafe credential file")
                os.fchmod(handle.fileno(), 0o600)
                return handle.read()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError):
            raise InfrastructureError("Unable to read credential.") from None

    def set(self, identifier, value):
        path = self._path(identifier)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(dir=self.directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, UnicodeError):
            raise InfrastructureError("Unable to save credential.") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def delete(self, identifier):
        try:
            self._path(identifier).unlink(missing_ok=True)
        except OSError:
            raise InfrastructureError("Unable to delete credential.") from None
