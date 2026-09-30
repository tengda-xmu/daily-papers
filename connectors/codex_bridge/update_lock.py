"""Reentrant thread + process lock shared by the bridge and Windows tasks."""
from contextlib import AbstractContextManager
import os
import threading
import time


class UpdateLock(AbstractContextManager):
    def __init__(self, path):
        self.path = path
        self.thread = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self.thread.acquire()
        if self.depth:
            self.depth += 1
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open('a+b')
            deadline = time.monotonic() + 60
            while True:
                try:
                    self.file.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Update check is already running') from None
                    time.sleep(.1)
            if not os.fstat(self.file.fileno()).st_size:
                self.file.write(b'0'); self.file.flush()
            self.depth = 1
            return self
        except BaseException:
            if getattr(self, 'file', None):
                self.file.close()
            self.thread.release()
            raise

    def __exit__(self, *args):
        self.depth -= 1
        if not self.depth:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
        self.thread.release()
