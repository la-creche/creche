#!/usr/bin/env python3
"""SPIKE (decision CH51, Hyperion check): the launcher of mechanism X.

  python3 -I xlaunch.py <profile> <program> [<arg>...]

It stands in for the unconfined launch child of the vmm. It keeps the
order of the CI report:

1. It writes `exec <profile>` to /proc/self/attr/apparmor/exec (the
   AppArmor change_onexec call). The kernel then puts the next exec of
   this process under <profile>.
2. It sets no_new_privs with prctl(PR_SET_NO_NEW_PRIVS), and checks it
   with PR_GET_NO_NEW_PRIVS.
3. It execs <program> in place, so the pid stays the same.

The kit applies no Landlock ruleset, so the step "apply Landlock" of the
vmm has no stand-in here.

It fails closed. If the write or the prctl fails, it prints
`xlaunch: <step>: <errno name>` and exits 126 with no exec, so no
program ever starts unconfined in place of the slot label. ENOENT on the
write means the kernel holds no profile of that name.

It opens no file but the attr, and Python opens its own files with
O_CLOEXEC, so the program gets only the fds that the caller gave.
"""

import ctypes
import errno
import os
import sys

ATTRS = ("/proc/self/attr/apparmor/exec", "/proc/self/attr/exec")
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39


def fail(step, number):
    name = errno.errorcode.get(number, f"errno{number}")
    print(f"xlaunch: {step}: {name}", file=sys.stderr)
    sys.exit(126)


def change_onexec(profile):
    for attr in ATTRS:
        try:
            fd = os.open(attr, os.O_WRONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        except OSError as error:
            fail("open exec attr", error.errno)
        try:
            os.write(fd, f"exec {profile}".encode())
        except OSError as error:
            fail("write exec attr", error.errno)
        finally:
            os.close(fd)
        return
    fail("open exec attr", errno.ENOENT)


def no_new_privs():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        fail("prctl no_new_privs", ctypes.get_errno())
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        fail("check no_new_privs", ctypes.get_errno() or errno.EPERM)


def main():
    if len(sys.argv) < 3:
        print("usage: xlaunch.py <profile> <program> [<arg>...]", file=sys.stderr)
        return 2
    profile, program = sys.argv[1], sys.argv[2]
    change_onexec(profile)
    no_new_privs()
    try:
        os.execv(program, sys.argv[2:])
    except OSError as error:
        fail("exec", error.errno)
    return 126


if __name__ == "__main__":
    sys.exit(main())
