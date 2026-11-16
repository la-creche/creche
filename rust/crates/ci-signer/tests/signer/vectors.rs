//! The shared vectors in `vectors/`. This crate is the authority: a case
//! says what the Rust code does. Root's Python reader (PR 4 of section 8)
//! and the guest helper (PR 5) run the same files. Do not change a vector
//! to pass a test.

use creche_ci_signer::base64_encode;
use creche_ci_signer::exchange::Request;
use creche_ci_signer::message::{MessageSource, UnverifiedTagMessage};
use creche_ci_signer::statement::Statement;
use serde::Deserialize;

use crate::common::crate_dir;

fn read<T: for<'de> Deserialize<'de>>(file: &str) -> T {
    let text = std::fs::read_to_string(crate_dir().join("vectors").join(file)).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StatementCase {
    json: String,
    written: Option<String>,
    note: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StatementFile {
    #[serde(rename = "Statement")]
    statement: Vec<StatementCase>,
}

#[test]
fn statement_vectors() {
    let file: StatementFile = read("statement.json");
    assert!(file.statement.len() > 20);
    for case in file.statement {
        let written = Statement::from_canonical(case.json.as_bytes())
            .ok()
            .map(|statement| String::from_utf8(statement.canonical_bytes()).unwrap());
        assert_eq!(written, case.written, "{}", case.note);
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageCase {
    message: String,
    source: String,
    statement_b64: Option<String>,
    bundle_b64: Option<String>,
    note: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageFile {
    #[serde(rename = "TagMessage")]
    tag_message: Vec<MessageCase>,
}

#[test]
fn tag_message_vectors() {
    let file: MessageFile = read("tag-message.json");
    for case in file.tag_message {
        let source = match case.source.as_str() {
            "tags_api" => MessageSource::TagsApi,
            "git_tag_object" => MessageSource::GitTagObject,
            other => panic!("{other}"),
        };
        let decoded = UnverifiedTagMessage::decode(&case.message, source).ok();
        let lines = decoded.as_ref().map(|message| {
            (
                base64_encode(message.unverified_statement()),
                base64_encode(message.bundle().bytes()),
            )
        });
        let expected = case.statement_b64.clone().zip(case.bundle_b64.clone());
        assert_eq!(lines, expected, "{}", case.note);

        // The lines of a decoded message give the message as written.
        if let Some((statement, bundle)) = lines {
            assert_eq!(
                Ok(format!("release-statement-1\n{statement}\n{bundle}").as_str()),
                source.written(&case.message),
                "{}",
                case.note
            );
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestCase {
    line: String,
    refused: Option<String>,
    tags: Option<String>,
    note: String,
}

/// The requests of `vectors/exchange.json`. The unit test `answer_vectors`
/// of `exchange.rs` reads its answers: a signed answer is made only from an
/// issued statement, which this test cannot build from a vector.
#[derive(Deserialize)]
struct ExchangeFile {
    requests: Vec<RequestCase>,
}

#[test]
fn exchange_vectors() {
    let file: ExchangeFile = read("exchange.json");
    for case in file.requests {
        let parsed = Request::parse(case.line.as_bytes());
        let refused = parsed
            .as_ref()
            .err()
            .map(|error| error.answer_code().word().to_owned());
        let tags = parsed.ok().map(|request| request.tags().canonical_json());
        assert_eq!((refused, tags), (case.refused, case.tags), "{}", case.note);
    }
}
