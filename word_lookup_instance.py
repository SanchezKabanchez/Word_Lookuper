"""Single process per profile, with activation of the existing GUI."""
import hashlib
import os
from pathlib import Path
import sys
import time
import uuid
from typing import BinaryIO


class SingleInstance:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.acquired = False
        self._file: BinaryIO | None = None
        self._mutex = self._event = None
        self._last_poll = 0.0
        self._seen = b""
        self._activation_file = self.directory / "activate.request"

    def acquire(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            return self._acquire_windows()
        import fcntl
        try:
            self._seen = self._activation_file.read_bytes()[:128]
        except FileNotFoundError:
            self._seen = b""
        self._file = (self.directory / "instance.lock").open("a+b")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False
        self.acquired = True
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(os.getpid()).encode("ascii"))
        self._file.flush()
        return True

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes
        kernel = self._kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.SetEvent.restype = kernel.ReleaseMutex.restype = kernel.CloseHandle.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        identity = hashlib.sha256(str(self.directory.resolve()).casefold().encode()).hexdigest()[:32]
        prefix = "Local\\WordLookup-" + identity
        getattr(ctypes, "set_last_error")(0)
        self._mutex = kernel.CreateMutexW(None, True, prefix)
        if not self._mutex:
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        self.acquired = getattr(ctypes, "get_last_error")() != 183  # ERROR_ALREADY_EXISTS
        self._event = kernel.CreateEventW(None, False, False, prefix + "-activate")
        if not self._event:
            error = getattr(ctypes, "get_last_error")()
            self.close()
            raise getattr(ctypes, "WinError")(error)
        return self.acquired

    def request_activation(self) -> None:
        if sys.platform.startswith("win"):
            if not self._event or not self._kernel.SetEvent(self._event):
                raise OSError("Could not signal the running Word Lookup instance")
        else:
            self._activation_file.write_bytes(uuid.uuid4().hex.encode("ascii"))

    def activation_requested(self) -> bool:
        if sys.platform.startswith("win"):
            return bool(self._event and self._kernel.WaitForSingleObject(self._event, 0) == 0)
        # Reuse the existing GUI event pump; one tiny local read per second.
        if time.monotonic() - self._last_poll < 1:
            return False
        self._last_poll = time.monotonic()
        try:
            value = self._activation_file.read_bytes()[:128]
        except FileNotFoundError:
            return False
        changed, self._seen = value != self._seen, value
        return changed

    def close(self) -> None:
        if self._file is not None:
            self._file.close()  # Do not unlink the inode on which peers lock.
            self._file = None
        if self._mutex:
            if self.acquired:
                self._kernel.ReleaseMutex(self._mutex)
            self._kernel.CloseHandle(self._mutex)
            self._mutex = None
        if self._event:
            self._kernel.CloseHandle(self._event)
            self._event = None
        self.acquired = False
