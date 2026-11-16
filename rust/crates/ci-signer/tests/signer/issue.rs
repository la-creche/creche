//! One statement for each run (section 4.5 step 6, section 4.6), on an
//! `Eligible` value that the recorded replies give.

use creche_ci_signer::base64_encode;
use creche_ci_signer::eligibility::{Eligible, decide_merge, decide_run};
use creche_ci_signer::exchange::{Answer, Request};
use creche_ci_signer::issue::{AlreadyIssued, IssuedRuns, JournalError};
use creche_ci_signer::message::{Bundle, MessageSource, TagMessage, UnverifiedTagMessage};
use creche_ci_signer::signed_at::SignedAt;
use creche_ci_signer::statement::{Branch, Event, Statement};

use crate::common::{MERGE_COMMIT, key, pairs_with, reply, running};

fn eligible() -> Eligible {
    let checked = decide_run(
        &pairs_with("probe.yml"),
        &key(48),
        &running("p1-run-push-main.json"),
    )
    .unwrap();
    decide_merge(checked, &reply("p1-commit-pull-merge-sha.json")).unwrap()
}

fn request() -> Request {
    Request::parse(br#"{"v":1,"tags":[{"tag":"handover-v0.1.96","assets":[]}]}"#).unwrap()
}

fn now() -> SignedAt {
    "2026-11-16T08:00:00Z".parse().unwrap()
}

#[test]
fn statement_takes_its_facts_from_eligible() {
    let mut runs = IssuedRuns::from_journal("").unwrap();
    runs.admit(&eligible()).unwrap();

    let issued = runs.issue(eligible(), request(), now()).unwrap();
    let statement = Statement::from_canonical(issued.signable.bytes()).unwrap();
    assert_eq!(
        statement.repository().to_string(),
        "example-owner/runner-test"
    );
    assert_eq!(statement.workflow().as_str(), "probe.yml");
    assert_eq!(statement.commit().as_str(), MERGE_COMMIT);
    assert_eq!(statement.run().get(), 48);
    assert_eq!(statement.event(), Event::Push);
    assert_eq!(statement.branch(), Branch::Main);
    assert_eq!(statement.signed_at(), now());
    assert_eq!(statement.tags(), request().tags());

    assert_eq!(issued.line.run.get(), 48);
    assert_eq!(issued.line.commit.as_str(), MERGE_COMMIT);
    let line = issued.line.to_string();
    assert!(
        line.starts_with(&format!("48\t{MERGE_COMMIT}\t")) && line.ends_with('\n'),
        "{line}"
    );
}

/// The whole signer path past the decision: issue, a tag message, the
/// answer line, and the read back that root does.
#[test]
fn signed_answer_of_an_issued_run() {
    let mut runs = IssuedRuns::from_journal("").unwrap();
    let issued = runs.issue(eligible(), request(), now()).unwrap();
    let statement = base64_encode(issued.signable.bytes());
    let bundle = Bundle::new(b"opaque bundle".to_vec()).unwrap();

    let message = TagMessage::new(issued.signable, bundle.clone());
    let text = message.encode();
    let read = UnverifiedTagMessage::decode(&text, MessageSource::TagsApi).unwrap();
    assert_eq!(read.unverified_statement(), message.statement());
    assert_eq!(read.bundle(), &bundle);
    let read_back = Statement::from_canonical(read.unverified_statement()).unwrap();
    assert_eq!(read_back.run().get(), 48);

    assert_eq!(
        String::from_utf8(Answer::Signed(message).to_line()).unwrap(),
        format!(
            "{{\"v\":1,\"signed\":{{\"statement\":\"{statement}\",\"bundle\":\"{}\"}}}}\n",
            base64_encode(bundle.bytes())
        )
    );
}

#[test]
fn second_issue_is_refused() {
    let mut runs = IssuedRuns::from_journal("").unwrap();
    let first = runs.issue(eligible(), request(), now()).unwrap();

    let run = 48.try_into().unwrap();
    assert_eq!(
        runs.issue(eligible(), request(), now()).unwrap_err(),
        AlreadyIssued::Run(run)
    );
    assert_eq!(
        runs.admit(&eligible()).unwrap_err(),
        AlreadyIssued::Run(run)
    );

    // A restart reads the journal, and the rule still holds.
    let restarted = IssuedRuns::from_journal(&first.line.to_string()).unwrap();
    assert_eq!(
        restarted.admit(&eligible()).unwrap_err(),
        AlreadyIssued::Run(run)
    );
}

#[test]
fn second_statement_of_a_commit_is_refused() {
    // A rerun under a new run id: the journal holds run 47 at the same
    // merge commit.
    let journal = format!("47\t{MERGE_COMMIT}\t{}\n", "a".repeat(64));
    let mut runs = IssuedRuns::from_journal(&journal).unwrap();
    let commit = MERGE_COMMIT.parse().unwrap();
    assert_eq!(
        runs.admit(&eligible()).unwrap_err(),
        AlreadyIssued::Commit(commit)
    );
    assert!(runs.issue(eligible(), request(), now()).is_err());
}

#[test]
fn journal_refusals() {
    let digest = "a".repeat(64);
    let line = format!("48\t{MERGE_COMMIT}\t{digest}\n");
    let other = "b".repeat(40);
    assert!(IssuedRuns::from_journal(&format!("{line}49\t{other}\t{digest}\n")).is_ok());
    assert_eq!(
        IssuedRuns::from_journal(&format!("{line}49\t{MERGE_COMMIT}\t{digest}\n")).unwrap_err(),
        JournalError::Duplicate(2)
    );

    let refuse = |text: &str| IssuedRuns::from_journal(text).unwrap_err();
    assert_eq!(refuse(line.trim_end()), JournalError::NoFinalLf);
    assert_eq!(refuse(&format!("{line}{line}")), JournalError::Duplicate(2));
    assert_eq!(refuse(&format!("{line}\n")), JournalError::Fields(2));
    assert_eq!(
        refuse(&format!("48\t{MERGE_COMMIT}\n")),
        JournalError::Fields(1)
    );
    assert!(matches!(
        refuse(&format!("0\t{MERGE_COMMIT}\t{digest}\n")),
        JournalError::Run(1, _)
    ));
    assert!(matches!(
        refuse(&format!("048\t{MERGE_COMMIT}\t{digest}\n")),
        JournalError::Run(1, _)
    ));
    assert!(matches!(
        refuse(&format!("48\tmain\t{digest}\n")),
        JournalError::Name(1, _)
    ));
    assert!(matches!(
        refuse(&format!("48\t{MERGE_COMMIT}\t{}\n", digest.to_uppercase())),
        JournalError::Name(1, _)
    ));
}
