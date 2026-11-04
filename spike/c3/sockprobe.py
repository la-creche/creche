#!/usr/bin/env python3
"""SPIKE (decision CH51, Hyperion check): the probe helpers of probes.sh.

Each mode prints one line of key=value words and exits 0, so probes.sh
decides PASS or FAIL. A wrong call exits 2.

- static <file>: exits 0 if file is an ELF program with no PT_INTERP, so
  it maps no library. ldd is not used: it runs a static-pie program.
- connect: the --tpm probe of tests/kvm_apparmor.rs. Mechanism H: CH
  starts through `setpriv --no-new-privs` at a slot link, so the slot
  profile attaches by the exec path. Mechanism X (--onexec <profile>): CH
  starts through xlaunch.py, which writes the onexec attr, sets
  no_new_privs and execs the one installed CH. CH connects to the --tpm
  socket while it makes the VM,
  with no guest program. With --listen, a test listener at the target
  accepts and closes each connect, so CH exits. The line says whether a
  connect came, the exit code, and whether CH logged EACCES.
- idle: CH with --api-socket only and no kernel, under H or X as above. CH binds the socket and
  waits for an API call. The line gives the label, whether the socket got
  bound, and EACCES. CH is killed after.
- onexec <profile>...: the cause of an aa-exec failure, which aa-exec's
  own message does not give reliably. For each profile it writes
  `exec <profile>` to the AppArmor exec attr of this process, which is the
  call that aa-exec makes, and it never execs. The line has one
  <profile>=<result> word per profile: ok, or the errno name of the write.
  ENOENT means the kernel holds no profile of that name.
"""

import argparse
import errno
import os
import pathlib
import socket
import struct
import subprocess
import sys
import time

SETPRIV = "/usr/bin/setpriv"
XLAUNCH = str(pathlib.Path(__file__).with_name("xlaunch.py"))
DEADLINE = 30.0
TICK = 0.02


def label_of(pid):
    for attr in (f"/proc/{pid}/attr/apparmor/current", f"/proc/{pid}/attr/current"):
        try:
            return pathlib.Path(attr).read_text().strip()
        except OSError:
            continue
    return "gone"


def static(path):
    data = pathlib.Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return False
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    for index in range(phnum):
        (kind,) = struct.unpack_from("<I", data, phoff + index * phentsize)
        if kind == 3:  # PT_INTERP
            return False
    return True


def onexec(profiles):
    words = []
    for name in profiles:
        words.append(f"{name}={change_onexec(name)}")
    print(" ".join(words))


def change_onexec(name):
    for attr in ("/proc/self/attr/apparmor/exec", "/proc/self/attr/exec"):
        try:
            fd = os.open(attr, os.O_WRONLY)
        except FileNotFoundError:
            continue
        except OSError as error:
            return errno.errorcode.get(error.errno, f"errno{error.errno}")
        try:
            os.write(fd, f"exec {name}".encode())
            return "ok"
        except OSError as error:
            return errno.errorcode.get(error.errno, f"errno{error.errno}")
        finally:
            os.close(fd)
    return "noattr"


def eacces(log):
    text = pathlib.Path(log).read_text(errors="replace")
    return "Permission denied" in text or "os error 13" in text


def listener(path):
    target = pathlib.Path(path)
    if target.exists() or target.is_symlink():
        sys.exit(f"sockprobe: {path} exists")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(8)
    server.setblocking(False)
    return server


def accept(server):
    if server is None:
        return False
    try:
        conn, _ = server.accept()
    except BlockingIOError:
        return False
    conn.close()
    return True


def spawn(args, log, onexec=None):
    """CH under mechanism H (onexec None) or X (onexec names the profile)."""
    if onexec is None:
        launcher = [SETPRIV, "--no-new-privs"]
    else:
        launcher = [sys.executable, "-I", XLAUNCH, onexec]
    with open(log, "wb") as out:
        return subprocess.Popen(
            [*launcher, *args],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def stop(process):
    if process.poll() is None:
        process.kill()
    return process.wait()


def connect(args):
    server = listener(args.target) if args.listen else None
    process = spawn(
        [
            args.exe,
            "--kernel",
            args.kernel,
            "--initramfs",
            args.initramfs,
            "--cmdline",
            "console=ttyS0 rdinit=/init",
            "--cpus",
            "boot=1",
            "--memory",
            "size=256M",
            "--serial",
            "off",
            "--console",
            "off",
            "--tpm",
            f"socket={args.target}",
        ],
        args.log,
        args.onexec,
    )
    label = "unseen"
    connected = False
    deadline = time.monotonic() + DEADLINE
    status = None
    try:
        while True:
            seen = label_of(process.pid)
            if seen not in ("gone", "unconfined"):
                label = seen
            connected |= accept(server)
            status = process.poll()
            if status is not None:
                break
            if time.monotonic() > deadline:
                status = f"timeout({stop(process)})"
                break
            time.sleep(TICK)
        # A connect that CH made just before it exited waits in the backlog.
        connected |= accept(server)
    finally:
        stop(process)
        if server is not None:
            server.close()
            os.unlink(args.target)
    label = label.replace(" ", "_")
    print(
        f"connected={'yes' if connected else 'no'} exit={status} "
        f"eacces={'yes' if eacces(args.log) else 'no'} label={label}"
    )


def idle(args):
    api = pathlib.Path(args.api)
    process = spawn([args.exe, "--api-socket", f"path={args.api}"], args.log, args.onexec)
    label = "gone"
    bound = False
    deadline = time.monotonic() + 10.0
    while process.poll() is None and time.monotonic() < deadline:
        if api.is_socket():
            bound = True
            label = label_of(process.pid)
            break
        time.sleep(TICK)
    status = process.poll()
    stop(process)
    if api.is_socket():
        api.unlink()
    lock = pathlib.Path(f"{args.api}.lock")
    if lock.exists():
        lock.unlink()
    label = label.replace(" ", "_")
    print(
        f"bound={'yes' if bound else 'no'} exit={'running' if status is None else status} "
        f"eacces={'yes' if eacces(args.log) else 'no'} label={label}"
    )


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="mode", required=True)
    mode = modes.add_parser("static")
    mode.add_argument("file")
    mode = modes.add_parser("connect")
    for flag in ("--exe", "--kernel", "--initramfs", "--target", "--log"):
        mode.add_argument(flag, required=True)
    mode.add_argument("--listen", action="store_true")
    mode.add_argument("--onexec", metavar="PROFILE")
    mode = modes.add_parser("idle")
    for flag in ("--exe", "--api", "--log"):
        mode.add_argument(flag, required=True)
    mode.add_argument("--onexec", metavar="PROFILE")
    mode = modes.add_parser("onexec")
    mode.add_argument("profiles", nargs="+")
    args = parser.parse_args()
    if args.mode == "onexec":
        onexec(args.profiles)
        return 0
    if args.mode == "static":
        return 0 if static(args.file) else 1
    if args.mode == "connect":
        connect(args)
    else:
        idle(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
