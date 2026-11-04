#!/usr/bin/env python3
"""SPIKE (decision CH51, Hyperion check): makes the spike slot profiles.

It mirrors rust/crates/vmm-confine/src/slot_profile.rs of the CI spike
(branch spike/c3-slot-pool). It reads the rule lines of base.profile, a
copy of rust/crates/vmm-confine/apparmor/cloud-hypervisor.profile at
origin/vmm/c-kvm-vm 14e247bf, and drops the comments.

The file holds two sets of 64 profiles, one per mechanism, so David
still loads one file:

- H, hard-link attach: creche-c3spike-slot-<k>. Each attaches to its own
  hard link /home/david/c3-spike/bin/ch-slot/<k>/cloud-hypervisor.
- X, change_onexec: creche-c3spike-xslot-<k>. Each has no attachment, so
  only an onexec write of its name enters it (xlaunch.py, aa-exec). Its
  binary rule names the one installed CH, /home/david/c3-spike/bin/
  cloud-hypervisor. Its run root is /home/david/c3-spike/runx/, so the two
  sets share no socket path.

It changes exactly four kinds of line, as the Rust generator does:

1. The header: the slot name, with the link path (H) or no path (X).
2. The rule of the binary: the link path (H) or the installed CH in the
   scratch dir (X) in place of the install path.
3. The signal rule between CH processes: peer=<the slot name>.
4. Each run dir rule: /home/david/c3-spike/run/slot/<k>/ (H) or
   /home/david/c3-spike/runx/slot/<k>/ (X) in place of
   /run/user/[0-9]*/vmm/vm/*/.

The Hyperion kit differs from the Rust generator in three ways only:
- The name prefix is creche-c3spike-slot-, so no spike label equals a
  real label.
- The links and the run root sit in the scratch dir, so the profiles
  attach to no binary outside it and allow no socket outside it.
- Each slot also gets `/home/david/c3-spike/bin/busybox mr,`, the probe
  rule of `render_probe`. aa-exec then can run busybox under a slot label
  for the signal test T5 and the load test T6. It adds no socket rule and
  no signal rule. One file with this rule in every slot keeps David's sudo
  steps at two.

A base that lacks one of these lines, or a result that still names the
base label, the install path, /run/user/ or /vm/, is an error.

  python3 -I gen-profiles.py --slots 64 > c3spike-slots.profile
  python3 -I gen-profiles.py --slots 64 --check c3spike-slots.profile
"""

import argparse
import hashlib
import pathlib
import sys

ROOT = "/home/david/c3-spike"  # main() replaces it with --root
PREFIX = "creche-c3spike-slot-"
X_PREFIX = "creche-c3spike-xslot-"
X_BINARY = f"{ROOT}/bin/cloud-hypervisor"
BASE_NAME = "creche-cloud-hypervisor"
BASE_BINARY = "/opt/components/vmm/bin/cloud-hypervisor"
BASE_RUN_DIR = "/run/user/[0-9]*/vmm/vm/*/"
ABI = "abi <abi/4.0>,"
BASE_SIGNAL = "signal (send, receive) peer=creche-cloud-hypervisor,"
RUN_DIR_RULES = 5
PROBE = f"{ROOT}/bin/busybox"
LEFT = (BASE_NAME, BASE_BINARY, BASE_RUN_DIR, "/run/user/", "/opt/components", "/vm/")


def set_root(root):
    """Point every scratch path at root (the CI spike uses the runner's home)."""
    global ROOT, X_BINARY, PROBE
    ROOT = root
    X_BINARY = f"{ROOT}/bin/cloud-hypervisor"
    PROBE = f"{ROOT}/bin/busybox"


class RenderError(Exception):
    """The base does not have the lines that the generator reads."""


def rule_lines(base):
    """The rule lines of base: trimmed, no blank line, no comment."""
    for line in base.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def count(base, what, want, kind):
    found = sum(1 for line in rule_lines(base) if kind(line))
    if found != want:
        raise RenderError(f"the base profile has {found} {what} lines, the generator reads {want}")


def render(base, slot, mechanism="H"):
    """The profile block of one slot of mechanism H or X, with no ABI line."""
    header = f"profile {BASE_NAME} {BASE_BINARY} {{"
    binary_rule = f"{BASE_BINARY} mr,"
    count(base, "header", 1, lambda line: line == header)
    count(base, "binary", 1, lambda line: line == binary_rule)
    count(base, "abi", 1, lambda line: line == ABI)
    count(base, "signal", 1, lambda line: line == BASE_SIGNAL)
    count(base, "run dir", RUN_DIR_RULES, lambda line: BASE_RUN_DIR in line)
    count(base, "end", 1, lambda line: line == "}")

    if mechanism == "H":
        name = f"{PREFIX}{slot}"
        binary = f"{ROOT}/bin/ch-slot/{slot}/cloud-hypervisor"
        new_header = f"profile {name} {binary} {{"
        run_dir = f"{ROOT}/run/slot/{slot}/"
    else:
        name = f"{X_PREFIX}{slot}"
        binary = X_BINARY
        new_header = f"profile {name} {{"
        run_dir = f"{ROOT}/runx/slot/{slot}/"
    block = []
    for line in rule_lines(base):
        if line == ABI:
            continue
        if line == header:
            rule = new_header
        elif line == binary_rule:
            rule = f"  {binary} mr,"
        elif line == BASE_SIGNAL:
            rule = f"  signal (send, receive) peer={name},"
        elif line == "}":
            rule = f"  {PROBE} mr,\n}}"
        else:
            rule = "  " + line.replace(BASE_RUN_DIR, run_dir)
        if any(word in rule for word in LEFT):
            raise RenderError(f"a slot rule still names the base: {rule}")
        block.append(rule)
    return "\n".join(block) + "\n"


def render_set(base, slots, base_sha):
    head = (
        "# SPIKE (decision CH51, Hyperion check). Made by gen-profiles.py of\n"
        "# the c3-hyperion-kit from base.profile, a copy of\n"
        "# rust/crates/vmm-confine/apparmor/cloud-hypervisor.profile at\n"
        f"# origin/vmm/c-kvm-vm 14e247bf (sha256 {base_sha}).\n"
        f"# H: {slots} profiles {PREFIX}0 to {PREFIX}{slots - 1}. Each attaches\n"
        f"# only to {ROOT}/bin/ch-slot/<k>/cloud-hypervisor.\n"
        f"# X: {slots} profiles {X_PREFIX}0 to {X_PREFIX}{slots - 1}. They\n"
        "# attach to no path. Only an onexec write of the name enters one.\n"
        "# Do not edit. David loads it with sudo apparmor_parser -r and\n"
        "# unloads it with sudo apparmor_parser -R. Agents never load it.\n"
        "\n"
        f"{ABI}\n"
    )
    return head + "".join(
        "\n" + render(base, slot, mechanism) for mechanism in ("H", "X") for slot in range(slots)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slots", type=int, required=True)
    parser.add_argument("--base", default=str(pathlib.Path(__file__).with_name("base.profile")))
    parser.add_argument("--check", metavar="FILE", help="fail unless FILE equals the output")
    parser.add_argument("--root", default=ROOT, help="the scratch dir the profiles name (CI only)")
    args = parser.parse_args()
    if not args.root.startswith("/") or args.root.endswith("/") or ".." in args.root:
        parser.error("--root must be an absolute path with no trailing slash and no '..'")
    set_root(args.root)
    if not 1 <= args.slots <= 64:
        parser.error("--slots must be 1 to 64")
    raw = pathlib.Path(args.base).read_bytes()
    try:
        text = render_set(raw.decode(), args.slots, hashlib.sha256(raw).hexdigest())
    except RenderError as error:
        print(f"gen-profiles: {error}", file=sys.stderr)
        return 1
    if args.check is None:
        sys.stdout.write(text)
        return 0
    if pathlib.Path(args.check).read_text() != text:
        print(f"gen-profiles: {args.check} differs from the generator output", file=sys.stderr)
        return 1
    print(f"gen-profiles: {args.check} equals the output for {args.slots} slots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
