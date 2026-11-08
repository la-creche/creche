# Sourced only (bin/AGENTS.md, Library): the source of the Cloud
# Hypervisor build of decision CH35 rule 3, the CH7-4 argv.
#
#   CH_BUILD          the 13 words of the build. cargo installs the
#                     programs cloud-hypervisor and ch-remote of the
#                     package cloud-hypervisor at the commit of the signed
#                     tag v54.0 (facts 54, 78). The caller names the
#                     install root with CARGO_INSTALL_ROOT and the target
#                     with CARGO_BUILD_TARGET, as build-artifacts.sh does
#                     for the command of a row.
#   CH_BUILD_VERSION  what `cloud-hypervisor --version` prints for that
#                     build: the crate version, because a `--rev` fetch
#                     brings no tag for `git describe` (fact 78).
#
# bin/vm-smoke-setup.sh runs the build for the kvm job of each workflow
# (decision CH75). The vmm manifest, rust/component.toml, holds the same
# words as its third build argv, because handover reads a build only from
# a manifest. bin/tests/test_vmm_manifest.py holds the two equal, so the
# kvm job tests the CH that the vmm release ships. bin/tests/
# test_artifactrows.py holds the rev equal to CH_REV and the version equal
# to CH_VERSION of rust/crates/vmm-store/src/key.rs, the two values that
# the key check of the vmm reads.
#
# Bash 3.2-clean: its tests source it on a development machine.
#
# The callers read both values, and no line here reads them.
# shellcheck disable=SC2034

CH_BUILD=(
  cargo install --locked --no-track
  --git https://github.com/cloud-hypervisor/cloud-hypervisor
  --rev 038ce5a72d761380a02f714a4eb4ca63e0ea073d cloud-hypervisor
  --bin cloud-hypervisor --bin ch-remote
)

CH_BUILD_VERSION="v54.0.0"
