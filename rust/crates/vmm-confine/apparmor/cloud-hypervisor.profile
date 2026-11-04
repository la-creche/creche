# The AppArmor profile of the Cloud Hypervisor binary of the vmm (decision
# CH8-1). Landlock confines the files of each VM (decisions CH8-3, CH30),
# but Landlock ABI 4 covers neither UDP nor Unix sockets (fact 77). This
# profile closes both: CH gets no inet socket at all, and no Unix socket
# but the sockets of the VM run dirs under the run root. One static
# profile cannot tell VMs apart, so each CH may reach the sockets of every
# VM, not only its own (CONTRACT-QUESTION at the run dir rules).
#
# Install (design section 6.1 step 1, gap G28 option A). David runs it on
# the host once, and again after each edit of this file:
#   sudo install -m 0644 rust/crates/vmm-confine/apparmor/cloud-hypervisor.profile \
#     /etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
#   sudo apparmor_parser -r /etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
# Check with `sudo aa-status | grep cloud-hypervisor`. Agents never install
# it on the host. The job kvm loads it on the runner (decision CH36 G20).
#
# A pathname Unix socket is mediated by the file rules: a connect or a
# bind needs `w` on the socket path. So the only write rules name the VM
# files of rust/crates/vmm-store/src/layout.rs (plan PR D1). The test
# tests/profile.rs holds each rule equal to those names. A rename there
# needs an edit here and a sudo reload (plan risk 8).

abi <abi/4.0>,

profile creche-cloud-hypervisor /opt/components/vmm/bin/cloud-hypervisor {
  # The binary itself. It is static, so it maps no library.
  /opt/components/vmm/bin/cloud-hypervisor mr,

  # The devices of fact 77, and the stdin that the launch step opens.
  /dev/kvm rw,
  /dev/urandom r,
  /dev/null rw,

  # Unix stream sockets only. An abstract address is denied both ways, so
  # the profile leaves CH only its own path sockets and socket pairs. No
  # `network` rule: CH can make no inet socket, TCP or UDP (decision CH8-1).
  unix type=stream,
  # The deny rules name only the permissions that carry an address. A
  # create has no address, so `deny unix addr=@**,` with no permission
  # list also denied every Unix socket create (C3 spike, PR #454).
  deny unix (bind, connect) addr=@**,
  deny unix (connect, send, receive) peer=(addr=@**),

  # Signals from the vmm and the user manager, and between CH threads.
  signal (receive) peer=unconfined,
  signal (send, receive) peer=creche-cloud-hypervisor,

  # The VM run dir under the run root `RuntimeDirectory=vmm` (decision
  # CH20, gap G5). `vmm.sock` sits in the run root itself, so no rule
  # names it (decision CH24). One pattern covers every host listener, so a
  # new port needs no reload (decision CH24). The rights follow the
  # Landlock run dir rules: no read of `serial.log` (gap G23).
  #
  # CONTRACT-QUESTION (decisions CH8-1, CH24): CH8-1 allows CH only "its
  # own" vsock and API sockets. CH24 decides the port wildcard, but no row
  # decides the VM id wildcard `vm/*/`. One static profile cannot name the
  # id of one VM, and Landlock ABI 4 does not cover AF_UNIX (fact 77). So
  # the CH of one family may connect to the `api.sock`, `vsock.sock` and
  # `vsock.sock_<port>` listeners of another family's VM. David decides
  # whether this reading of "its own" holds.
  owner /run/user/[0-9]*/vmm/vm/*/api.sock rw,
  owner /run/user/[0-9]*/vmm/vm/*/api.sock.lock wk,
  owner /run/user/[0-9]*/vmm/vm/*/vsock.sock rw,
  owner /run/user/[0-9]*/vmm/vm/*/vsock.sock_* rw,
  owner /run/user/[0-9]*/vmm/vm/*/serial.log w,

  # The state root names of decision CH39 rule 3. CONTRACT-QUESTION
  # (decisions CH8-1, CH39 rule 3): no row sets the state root default
  # (plan PR L20), so each rule matches its names under any root. This
  # widens the socket deny of CH8-1: `w` on a pathname socket allows a
  # connect, so CH may connect to any socket of the owner that sits at a
  # path `**/sessions/*/disk.ext4` or under `**/sessions/*/snapshots/`.
  # A rule that names one root needs a fixed root or a variable that the
  # install of design section 6.1 sets, and a sudo reload. The root disks
  # and kernels are read only (decision CH8-8).
  owner /**/store/* rk,
  owner /**/sessions/*/disk.ext4 rwk,
  owner /**/sessions/*/snapshots/** rw,
}
