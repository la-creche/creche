"""The secret intake: root writes a secret it cannot read.

`stage7-releases.md` §4.3, and the operator's decision — the intake is a
ROOT-OWNED endpoint on the LAN address.

The constraint it removes: the PEP's sops file is encrypted to recipients
that leave the host's operator account out on purpose. So nobody on the host
can decrypt that file to add a key to it, and a key minted on the host has
to be carried to a machine that can.

The fix is the file layout before it is the service. One file per secret,
`/var/lib/creche-handover/secrets/<name>.enc`, each encrypted to the same
recipient set. Adding a secret is then **encrypt only**, and encryption
needs public keys alone. The host writes a new secret while holding no
private key at all.

Three modules, and the split is deliberate.

- `token.py` — the capability. It holds no value and reaches no disk.
- `store.py` — the gap rule and the encrypt-only write. **It has no read
  verb, and §4.3 rule 4 says no read verb exists anywhere.**
- `service.py` — the HTTP surface. Stdlib only, LAN bound, TLS, and every
  answer is a fixed string.

Nothing in this package holds a private key, so a reader of this code
cannot find a path from it to a plaintext secret. That is the point.
"""
