"""Tie the resident Linux engine to AstrBot, including an unexpected host exit."""

import ctypes
import os
import signal
import sys


def main():
    parent, binary, *args = sys.argv[1:]
    # Set PR_SET_PDEATHSIG in a fresh, single-threaded process before exec.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() == int(parent):
        os.execv(binary, [binary, *args])


if __name__ == "__main__":
    main()
