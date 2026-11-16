# CI runner VMs on the host (`ci-runner`): moved

> Status as of 2026-11-16: Moved. The component lives in its own repository now.

The `ci-runner` component moved out of this repository. It runs one throwaway Cloud Hypervisor VM per Forgejo Actions job.

Its new home is the repository `ci-runner` in the organization `infra` on the Forgejo instance. Its design lives there as `docs/design.md`. Other notes of this repository, such as `forgejo-release-path.md`, cite sections of this note. Those citations name the sections of the full note as it stood at commit `5535f895`.

Why: the runner serves every repository on the Forgejo instance. It is generic infrastructure, so it is no part of this product. The operator decided on 2026-11-16 to keep it apart from creche.

The full note, the crate `rust/crates/ci-runner/` and the guest image build `ci-runner-image/` are in the history of this repository up to commit `5535f895`.
