//! The limits of a release statement and of its tag message
//! (`docs/design/forgejo-release-path.md`, section 4.5).
//!
//! The worst tag message is a constant of these limits. A compile-time
//! assertion holds it below `MAX_TAG_MESSAGE_BYTES`, and that below the
//! measured limit of Forgejo. So every statement that the types allow
//! fits in a tag message, and the encoder needs no size check.

use crate::names::{MAX_OWNER_BYTES, MAX_REPO_NAME_BYTES};

/// The most tags in one statement (section 4.5 step 4).
pub const MAX_TAGS: usize = 32;

/// The most assets of one tag (section 4.5 step 4).
pub const MAX_ASSETS_PER_TAG: usize = 16;

/// The most bytes of a tag name and of an asset name (section 4.5).
///
/// Decided by the operator on 2026-11-16.
pub const MAX_NAME_BYTES: usize = 64;

/// The most bytes of a workflow file name.
///
/// Decided by the operator on 2026-11-16. The cap gives the worst
/// statement a size. Each workflow of `signing-pairs.tsv` is far below it.
/// A run reply with a longer `workflow_id` is refused (fail closed). A
/// change is this constant, the `WorkflowFile` pattern and one vector.
pub const MAX_WORKFLOW_BYTES: usize = 64;

/// The most bytes of a cosign bundle (section 4.5).
///
/// Decided by the operator on 2026-11-16. cosign v3.1.4 with a P-256 key
/// and no log entry writes a bundle of 387 bytes (measured on 2026-11-16,
/// `vectors/cosign-3.1.4/`). The cap leaves 3709 bytes for another key
/// type or bundle form.
pub const MAX_BUNDLE_BYTES: usize = 4096;

/// The most bytes of a tag message that the signer writes (section 4.5).
///
/// Decided by the operator on 2026-11-16.
pub const MAX_TAG_MESSAGE_BYTES: usize = 120_000;

/// The largest message that `POST /repos/{o}/{r}/tags` of Forgejo 17.0.1
/// takes (section 4.5, measured on a scratch repository on 2026-11-16). A
/// message of 131072 bytes answers 500 `argument list too long`.
pub const FORGEJO_TAG_MESSAGE_LIMIT: usize = 131_071;

/// The first line of a tag message (section 4.5).
pub const MESSAGE_HEADER: &str = "release-statement-1";

/// The most decimal digits of a run id: 2^53 - 1 has 16.
const MAX_RUN_DIGITS: usize = 16;

/// The bytes of a `signed_at` value: `YYYY-MM-DDTHH:MM:SSZ`.
const SIGNED_AT_BYTES: usize = 20;

/// The bytes of a commit id.
const COMMIT_BYTES: usize = 40;

/// The bytes of a SHA-256 in hex.
const SHA256_HEX_BYTES: usize = 64;

/// The bytes of `n` items of `item` bytes each, with a comma between two.
const fn list(n: usize, item: usize) -> usize {
    if n == 0 {
        return 0;
    }
    n * item + (n - 1)
}

/// The bytes of the base64 form of `n` bytes, with padding.
pub(crate) const fn base64_len(n: usize) -> usize {
    n.div_ceil(3) * 4
}

/// The bytes of the largest asset object.
const WORST_ASSET_BYTES: usize =
    r#"{"name":"","sha256":""}"#.len() + MAX_NAME_BYTES + SHA256_HEX_BYTES;

/// The bytes of the largest tag object.
const WORST_TAG_BYTES: usize = r#"{"tag":"","assets":[]}"#.len()
    + MAX_NAME_BYTES
    + list(MAX_ASSETS_PER_TAG, WORST_ASSET_BYTES);

/// The bytes of the largest statement in its canonical form. The canonical
/// form is the key order of `Statement::canonical_bytes`, with no
/// whitespace.
pub const WORST_STATEMENT_BYTES: usize = r#"{"statement":1,"repository":"/","workflow":"","event":"push","ref":"main","commit":"","run":,"signed_at":"","tags":[]}"#
    .len()
    + MAX_OWNER_BYTES
    + MAX_REPO_NAME_BYTES
    + MAX_WORKFLOW_BYTES
    + COMMIT_BYTES
    + MAX_RUN_DIGITS
    + SIGNED_AT_BYTES
    + list(MAX_TAGS, WORST_TAG_BYTES);

/// The bytes of the largest tag message: the header, the statement and the
/// bundle in base64, with one LF between two lines.
pub const WORST_TAG_MESSAGE_BYTES: usize =
    MESSAGE_HEADER.len() + 1 + base64_len(WORST_STATEMENT_BYTES) + 1 + base64_len(MAX_BUNDLE_BYTES);

// The message cap of section 4.5 holds by construction: no statement that the
// types allow makes a message above the cap, and the cap is below the
// measured limit of Forgejo. A git tag object adds one LF, so the cap keeps
// a margin of more than one byte.
const _: () = assert!(WORST_TAG_MESSAGE_BYTES <= MAX_TAG_MESSAGE_BYTES);
const _: () = assert!(MAX_TAG_MESSAGE_BYTES < FORGEJO_TAG_MESSAGE_LIMIT);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worst_case_near_the_note() {
        // Section 4.5: "the largest message is about 113500 bytes".
        assert!(
            (110_000..=115_000).contains(&WORST_TAG_MESSAGE_BYTES),
            "{WORST_TAG_MESSAGE_BYTES}"
        );
        assert_eq!(base64_len(0), 0);
        assert_eq!(base64_len(1), 4);
        assert_eq!(base64_len(3), 4);
        assert_eq!(base64_len(4), 8);
    }
}
