"""OS-owned scan lease shared by dashboard and CLI; crashes release it."""
import os
from pathlib import Path


class ScanLock:
    def __init__(self, database):
        self.path = Path(str(database) + ".scan.lock").resolve()
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def busy(self):
        probe = ScanLock(self.path.as_posix().removesuffix(".scan.lock"))
        if not probe.acquire():
            return True
        probe.release()
        return False
