//! The request line and the answer line of section 4.5.

use creche_ci_signer::exchange::{Answer, AnswerCode, REQUEST_MAX_BYTES, Request, RequestError};
use creche_ci_signer::message::{MessageSource, TagMessage};
use creche_ci_signer::names::NameFault;
use creche_ci_signer::statement::LimitError;

const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

fn parse(line: &str) -> Result<Request, RequestError> {
    Request::parse(line.as_bytes())
}

#[test]
fn accepted_request() {
    let line = format!(
        r#"{{"v":1,"tags":[{{"tag":"vmm-v0.1.24","assets":[{{"name":"vmm.tar.gz","sha256":"{DIGEST}"}}]}},{{"tag":"handover-v0.1.96","assets":[]}}]}}"#
    );
    let request = parse(&line).unwrap();
    let tags: Vec<&str> = request
        .tags()
        .items()
        .iter()
        .map(|entry| entry.tag.as_str())
        .collect();
    assert_eq!(tags, ["handover-v0.1.96", "vmm-v0.1.24"], "sorted by tag");
}

#[test]
fn refused_requests() {
    let asset = format!(r#"{{"name":"a","sha256":"{DIGEST}"}}"#);
    let one_tag =
        |assets: &str| format!(r#"{{"v":1,"tags":[{{"tag":"vmm-v1.0.0","assets":[{assets}]}}]}}"#);

    let cases: Vec<(String, RequestError)> = vec![
        ("{\"v\":1,\"tags\":[]".to_owned(), RequestError::NotJson),
        (
            r#"{"v":1,"v":1,"tags":[]}"#.to_owned(),
            RequestError::NotJson,
        ),
        (r#"{"v":2,"tags":[]}"#.to_owned(), RequestError::Version),
        (r#"{"tags":[]}"#.to_owned(), RequestError::Shape),
        (
            r#"{"v":1,"tags":[],"run":48}"#.to_owned(),
            RequestError::Shape,
        ),
        (
            r#"{"v":1,"tags":[],"repository":"example-owner/creche"}"#.to_owned(),
            RequestError::Shape,
        ),
        (
            r#"{"v":1,"tags":[],"commit":"5791a8cdae55087207f12ab919c5eb964494d654"}"#.to_owned(),
            RequestError::Shape,
        ),
        (
            r#"{"v":1,"tags":[{"tag":"vmm-v1.0.0"}]}"#.to_owned(),
            RequestError::Shape,
        ),
        (
            r#"{"v":1,"tags":[{"tag":"vmm-v1.0.0","assets":[],"x":1}]}"#.to_owned(),
            RequestError::Shape,
        ),
        (
            r#"{"v":1,"tags":[]}"#.to_owned(),
            RequestError::Limits(LimitError::NoTag),
        ),
        (
            one_tag(&[asset.as_str(), asset.as_str()].join(",")),
            RequestError::Limits(LimitError::DuplicateAsset),
        ),
        (
            one_tag(&format!(
                r#"{{"name":"a","sha256":"{}"}}"#,
                DIGEST.to_uppercase()
            )),
            RequestError::Grammar(name_error("Sha256Hex", NameFault::Grammar)),
        ),
        (
            one_tag(&format!(r#"{{"name":"a/b","sha256":"{DIGEST}"}}"#)),
            RequestError::Grammar(name_error("AssetName", NameFault::Grammar)),
        ),
        (
            r#"{"v":1,"tags":[{"tag":"main","assets":[]}]}"#.to_owned(),
            RequestError::Grammar(name_error("ReleaseTag", NameFault::Grammar)),
        ),
        (
            r#"{"v":1,"tags":[{"tag":"vmm-v1234567890.0.0","assets":[]}]}"#.to_owned(),
            RequestError::Grammar(name_error("ReleaseTag", NameFault::Grammar)),
        ),
    ];
    for (line, expected) in cases {
        assert_eq!(parse(&line).unwrap_err(), expected, "{line}");
    }

    let assets: Vec<String> = (0..17)
        .map(|n| format!(r#"{{"name":"a{n}","sha256":"{DIGEST}"}}"#))
        .collect();
    assert_eq!(
        parse(&one_tag(&assets.join(","))).unwrap_err(),
        RequestError::Limits(LimitError::TooManyAssets)
    );

    let tags: Vec<String> = (0..33)
        .map(|n| format!(r#"{{"tag":"c{n:02}-v1.0.0","assets":[]}}"#))
        .collect();
    let line = format!(r#"{{"v":1,"tags":[{}]}}"#, tags.join(","));
    assert_eq!(
        parse(&line).unwrap_err(),
        RequestError::Limits(LimitError::TooManyTags)
    );
    let dup = format!(
        r#"{{"v":1,"tags":[{0},{0}]}}"#,
        r#"{"tag":"vmm-v1.0.0","assets":[]}"#
    );
    assert_eq!(
        parse(&dup).unwrap_err(),
        RequestError::Limits(LimitError::DuplicateTag)
    );

    let padded = format!("{}{}", one_tag(""), " ".repeat(REQUEST_MAX_BYTES));
    assert_eq!(parse(&padded).unwrap_err(), RequestError::TooLarge);
    assert_eq!(RequestError::TooLarge.answer_code(), AnswerCode::Limits);
    assert_eq!(RequestError::Shape.answer_code(), AnswerCode::Grammar);
}

fn name_error(kind: &'static str, fault: NameFault) -> creche_ci_signer::names::NameError {
    creche_ci_signer::names::NameError { kind, fault }
}

#[test]
fn answer_lines() {
    let refused = [
        (AnswerCode::Grammar, "grammar"),
        (AnswerCode::Limits, "limits"),
        (AnswerCode::AlreadyIssued, "already_issued"),
        (AnswerCode::NotEligible, "not_eligible"),
        (AnswerCode::Fault, "fault"),
    ];
    for (code, word) in refused {
        assert_eq!(
            Answer::Refused(code).to_line(),
            format!("{{\"v\":1,\"refused\":\"{word}\"}}\n").into_bytes()
        );
    }

    let message =
        TagMessage::decode("release-statement-1\neyJ9\nYg==", MessageSource::TagsApi).unwrap();
    assert_eq!(
        Answer::Signed(message).to_line(),
        b"{\"v\":1,\"signed\":{\"statement\":\"eyJ9\",\"bundle\":\"Yg==\"}}\n".to_vec()
    );
}
