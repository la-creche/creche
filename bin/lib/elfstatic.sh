# Sourced only (bin/AGENTS.md, Library): the one check that a program is
# static.
#
#   elf_static PATH            status 0 when PATH is an ELF program with no
#                              PT_INTERP header. A static program needs no
#                              program interpreter, and the guest has none.
#                              Else status 1 and one line on stderr.
#   elf_static_refuses_dynamic status 0 when the check refuses the dynamic
#                              /bin/bash of the runner: the positive control.
#                              A broken pattern makes the check pass every
#                              program, and this function then fails.
#
# bin/vm-smoke-setup.sh step 7 and step 11 run it on guest-init, the
# stand-in facade, CH and ch-remote. The vmm-archive job of gate.yml runs
# it on each program of the vmm archive (decision CH76 rule 4).
#
# It needs `readelf` of binutils, which a GitHub runner has.
#
# Bash 3.2-clean.

elf_static() {
  local headers

  if ! headers=$(readelf -lW "$1"); then
    printf 'elf_static: readelf cannot read %s\n' "$1" >&2
    return 1
  fi
  if grep -Eq '^[[:space:]]+INTERP[[:space:]]' <<< "$headers"; then
    printf 'elf_static: %s names a program interpreter: it is not static\n' "$1" >&2
    return 1
  fi
}

elf_static_refuses_dynamic() {
  if elf_static /bin/bash 2> /dev/null; then
    printf 'elf_static: the check is broken: it passes the dynamic /bin/bash\n' >&2
    return 1
  fi
}
