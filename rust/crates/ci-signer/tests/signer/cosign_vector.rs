//! The real cosign v3.1.4 files of `vectors/cosign-3.1.4/`: a public key,
//! a statement and its bundle, which verify together with cosign
//! (measured on 2026-11-16). This test needs no cosign. It checks that the
//! crate takes the real files and that the bundle names the statement and
//! the key. Root's verifier runs cosign on the same files.

use creche_ci_signer::base64_decode;
use creche_ci_signer::base64_encode;
use creche_ci_signer::message::{Bundle, MessageSource, UnverifiedTagMessage};
use creche_ci_signer::statement::Statement;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::common::crate_dir;

/// The bytes of a bundle of cosign v3.1.4 with a P-256 key and no log
/// entry (measured on 2026-11-16).
const MEASURED_BUNDLE_BYTES: usize = 387;

/// The media type of the measured bundle.
const BUNDLE_MEDIA_TYPE: &str = "application/vnd.dev.sigstore.bundle.v0.3+json";

fn vector(name: &str) -> Vec<u8> {
    std::fs::read(crate_dir().join("vectors/cosign-3.1.4").join(name)).unwrap()
}

/// The base64 of the SHA-256 of `bytes`, as a bundle writes it.
fn digest_b64(bytes: &[u8]) -> String {
    base64_encode(&Sha256::digest(bytes))
}

/// The DER bytes of a PEM `PUBLIC KEY` file.
fn pem_der(pem: &[u8]) -> Vec<u8> {
    let text = std::str::from_utf8(pem).unwrap();
    let body: String = text
        .lines()
        .filter(|line| !line.starts_with("-----"))
        .collect();
    assert!(text.starts_with("-----BEGIN PUBLIC KEY-----\n"));
    base64_decode(&body).unwrap()
}

#[test]
fn real_bundle_fits_the_crate() {
    let statement = vector("statement.json");
    let bundle = vector("statement.sigstore.json");

    // The statement is canonical: the crate writes the same bytes back.
    let read = Statement::from_canonical(&statement).unwrap();
    assert_eq!(read.canonical_bytes(), statement);

    // The bundle has the measured size and fits `Bundle`.
    assert_eq!(bundle.len(), MEASURED_BUNDLE_BYTES);
    assert!(Bundle::new(bundle.clone()).is_ok());

    // The bundle names the SHA-256 of the statement and of the key.
    let json: Value = serde_json::from_slice(&bundle).unwrap();
    assert_eq!(json["mediaType"], BUNDLE_MEDIA_TYPE);
    assert_eq!(
        json["messageSignature"]["messageDigest"]["digest"],
        digest_b64(&statement).as_str()
    );
    assert_eq!(
        json["verificationMaterial"]["publicKey"]["hint"],
        digest_b64(&pem_der(&vector("cosign.pub"))).as_str()
    );
}

#[test]
fn real_bundle_round_trips() {
    let statement = vector("statement.json");
    let bundle = vector("statement.sigstore.json");
    let message = format!(
        "release-statement-1\n{}\n{}",
        base64_encode(&statement),
        base64_encode(&bundle)
    );

    let decoded = UnverifiedTagMessage::decode(&message, MessageSource::TagsApi).unwrap();
    assert_eq!(decoded.unverified_statement(), statement.as_slice());
    assert_eq!(decoded.bundle().bytes(), bundle.as_slice());

    // `vectors/tag-message.json` holds the same message, so each reader
    // runs it.
    let text = std::fs::read_to_string(crate_dir().join("vectors/tag-message.json")).unwrap();
    let file: Value = serde_json::from_str(&text).unwrap();
    let cases = file["TagMessage"].as_array().unwrap();
    assert!(cases.iter().any(|case| case["message"] == message.as_str()));
}
