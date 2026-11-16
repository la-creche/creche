//! The readers of the Forgejo replies and the tag message facts, on the
//! recordings of Forgejo 17.0.1.

use creche_ci_signer::forgejo::{
    CommitPull, Event, FullRef, REPLY_MAX_BYTES, Reply, ReplyFault, RunStatus, TriggerEvent,
    parse_commit_pull, parse_run,
};
use creche_ci_signer::limits::FORGEJO_TAG_MESSAGE_LIMIT;
use creche_ci_signer::message::{MessageError, MessageSource, TagMessage};
use serde_json::Value;

use crate::common::{MERGE_COMMIT, REPOSITORY, json, message_of, mutated, reply};

fn branch(name: &str) -> Option<FullRef> {
    Some(FullRef::Branch(name.to_owned()))
}

fn tag(name: &str) -> Option<FullRef> {
    Some(FullRef::Tag(name.to_owned()))
}

/// Each run recording gives the facts of the table of section 4.4.
#[test]
fn run_facts_of_the_table() {
    let cases = [
        (
            "p1-run-push-main.json",
            48,
            "probe.yml",
            Event::Push,
            TriggerEvent::Push,
            branch("main"),
        ),
        (
            "p1-run-push-branch.json",
            56,
            "probe.yml",
            Event::Push,
            TriggerEvent::Push,
            branch("rec-release"),
        ),
        (
            "p1-run-push-tag.json",
            65,
            "probe.yml",
            Event::Push,
            TriggerEvent::Push,
            tag("rec-tag-push"),
        ),
        (
            "p1-run-push-tag-main.json",
            15,
            "test.yml",
            Event::Push,
            TriggerEvent::Push,
            tag("main"),
        ),
        (
            "p1-run-pull-request.json",
            70,
            "probe.yml",
            Event::PullRequest,
            TriggerEvent::PullRequest,
            None,
        ),
        (
            "p11-run-prt.json",
            69,
            "prt.yml",
            Event::PullRequest,
            TriggerEvent::PullRequestTarget,
            None,
        ),
        (
            "p1-run-workflow-dispatch.json",
            104,
            "rel.yml",
            Event::Empty,
            TriggerEvent::WorkflowDispatch,
            branch("rec-release"),
        ),
    ];

    for (name, id, workflow, event, trigger, payload_ref) in cases {
        let run = parse_run(&reply(name)).unwrap_or_else(|fault| panic!("{name}: {fault}"));
        assert_eq!(run.id.get(), id, "{name}");
        assert_eq!(run.repository.to_string(), REPOSITORY, "{name}");
        assert_eq!(run.workflow.as_str(), workflow, "{name}");
        assert_eq!(run.event, event, "{name}");
        assert_eq!(run.trigger, trigger, "{name}");
        assert_eq!(run.payload_ref, payload_ref, "{name}");
        assert_eq!(run.status, RunStatus::Success, "{name}");
        assert_eq!(
            run.commit.as_str(),
            json(name)["commit_sha"].as_str().unwrap(),
            "{name}"
        );
    }

    // The merge push and the tag push build the same commit.
    let main = parse_run(&reply("p1-run-push-main.json")).unwrap();
    let tagged = parse_run(&reply("p1-run-push-tag.json")).unwrap();
    assert_eq!(main.commit.as_str(), MERGE_COMMIT);
    assert_eq!(tagged.commit, main.commit);
}

/// One change of a recorded reply.
type Change = fn(&mut Value);

#[test]
fn run_faults() {
    let name = "p1-run-push-main.json";
    let fault = |reply: Reply| parse_run(&reply).unwrap_err();

    assert_eq!(
        fault(Reply {
            status: 404,
            ..reply(name)
        }),
        ReplyFault::Status(404)
    );

    let mut big = reply(name);
    big.body.resize(REPLY_MAX_BYTES + 1, b' ');
    assert_eq!(fault(big), ReplyFault::TooLarge);

    let mut twice = reply(name);
    assert!(twice.body.starts_with(b"{\"id\":48,"));
    twice.body.splice(0..1, b"{\"id\":48,".iter().copied());
    assert_eq!(fault(twice), ReplyFault::NotStrictJson);

    let cases: [(&str, Change, ReplyFault); 7] = [
        (
            "commit_sha",
            |run| drop(run.as_object_mut().unwrap().remove("commit_sha")),
            ReplyFault::Shape("commit_sha"),
        ),
        (
            "event_payload",
            |run| run["event_payload"] = Value::from("{not json"),
            ReplyFault::Shape("event_payload"),
        ),
        (
            "event_payload twice",
            |run| run["event_payload"] = Value::from(r#"{"ref":"a","ref":"b"}"#),
            ReplyFault::Shape("event_payload"),
        ),
        (
            "status",
            |run| run["status"] = Value::from("queued"),
            ReplyFault::Shape("status"),
        ),
        (
            "workflow_id",
            |run| run["workflow_id"] = Value::from(".forgejo/workflows/probe.yml"),
            ReplyFault::Shape("workflow_id"),
        ),
        (
            "id",
            |run| run["id"] = Value::from(0),
            ReplyFault::Shape("id"),
        ),
        (
            "full_name",
            |run| run["repository"]["full_name"] = Value::from("runner-test"),
            ReplyFault::Shape("repository.full_name"),
        ),
    ];
    for (what, change, expected) in cases {
        assert_eq!(fault(mutated(name, change)), expected, "{what}");
    }
}

#[test]
fn commit_pull_replies() {
    let CommitPull::Merge(pull) =
        parse_commit_pull(&reply("p1-commit-pull-merge-sha.json")).unwrap()
    else {
        panic!("the merge commit has a PR");
    };
    assert_eq!(pull.number, 2);
    assert!(pull.merged);
    assert_eq!(pull.base_ref, "main");
    assert_eq!(pull.merge_commit.unwrap().as_str(), MERGE_COMMIT);

    assert_eq!(reply("p1-commit-pull-pr-head.json").status, 404);
    assert_eq!(
        parse_commit_pull(&reply("p1-commit-pull-pr-head.json")).unwrap(),
        CommitPull::NoPull
    );

    let error = Reply {
        status: 500,
        ..reply("p1-commit-pull-merge-sha.json")
    };
    assert_eq!(
        parse_commit_pull(&error).unwrap_err(),
        ReplyFault::Status(500)
    );
    let bad = mutated("p1-commit-pull-merge-sha.json", |pull| {
        pull["merge_commit_sha"] = Value::from("5791a8")
    });
    assert_eq!(
        parse_commit_pull(&bad).unwrap_err(),
        ReplyFault::Shape("merge_commit_sha")
    );
}

/// M3 and recording fact 3: `git/tags/{sha}` adds one LF, and the tags API
/// gives the message as written. `MessageSource` holds that difference.
#[test]
fn message_sources_of_forgejo() {
    let written = message_of("p2-tag-create.json");
    assert!(!written.ends_with('\n'));
    assert_eq!(message_of("p2-tags-get.json"), written);

    let git_object = message_of("p2-git-tags-sha.json");
    assert_eq!(git_object, format!("{written}\n"));
    assert_eq!(
        MessageSource::GitTagObject.written(&git_object),
        Ok(written.as_str())
    );
    assert_eq!(
        MessageSource::TagsApi.written(&written),
        Ok(written.as_str())
    );
    assert_eq!(
        MessageSource::TagsApi.written(&git_object),
        Err(MessageError::Suffix)
    );
    assert_eq!(
        MessageSource::GitTagObject.written(&written),
        Err(MessageError::Suffix)
    );

    // The recorded message is no release statement.
    assert_eq!(
        TagMessage::decode(&git_object, MessageSource::GitTagObject),
        Err(MessageError::Lines)
    );
}

/// Section 4.5: `POST /tags` takes 131071 bytes and refuses 131072.
#[test]
fn forgejo_message_limit() {
    let largest = reply("p2-tag-size-b131071.json");
    assert_eq!(largest.status, 201);
    assert_eq!(
        message_of("p2-tag-size-b131071.json").len(),
        FORGEJO_TAG_MESSAGE_LIMIT
    );

    let refused = reply("p2-tag-size-b131072.json");
    assert_eq!(refused.status, 500);
    assert!(
        json("p2-tag-size-b131072.json")["message"]
            .as_str()
            .unwrap()
            .ends_with("argument list too long")
    );
}

#[test]
fn recorded_version() {
    assert_eq!(
        json("version.json")["version"].as_str().unwrap(),
        "17.0.1+gitea-1.22.0"
    );
}
