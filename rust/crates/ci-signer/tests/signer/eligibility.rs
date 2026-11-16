//! The checks of section 4.4 on the recorded runs. Each case uses pairs
//! that name the workflow of its run, so it isolates one check.

use creche_ci_signer::eligibility::{MergeRefusal, Refusal, RunKey, decide_merge, decide_run};
use creche_ci_signer::forgejo::{Reply, ReplyFault};
use creche_ci_signer::pairs::SigningPairs;
use serde_json::Value;

use crate::common::{MERGE_COMMIT, key, mutated, pairs_with, reply, running};

const MERGE_PULL: &str = "p1-commit-pull-merge-sha.json";

/// `decide_run`, then `decide_merge` with `pull`, as the signer runs them.
fn decide(pairs: &SigningPairs, key: &RunKey, run: &Reply, pull: &Reply) -> Result<u64, Refusal> {
    let checked = decide_run(pairs, key, run)?;
    let eligible = decide_merge(checked, pull)?;
    Ok(eligible.run().get())
}

#[test]
fn merge_push_to_main_is_eligible() {
    let run = running("p1-run-push-main.json");
    let checked = decide_run(&pairs_with("probe.yml"), &key(48), &run).unwrap();
    assert_eq!(checked.commit().as_str(), MERGE_COMMIT);
    assert_eq!(
        checked.repository().to_string(),
        "example-owner/runner-test"
    );

    let eligible = decide_merge(checked, &reply(MERGE_PULL)).unwrap();
    assert_eq!(eligible.run().get(), 48);
    assert_eq!(eligible.commit().as_str(), MERGE_COMMIT);
    assert_eq!(eligible.workflow().as_str(), "probe.yml");
    assert_eq!(
        eligible.repository().to_string(),
        "example-owner/runner-test"
    );
}

#[test]
fn waiting_is_eligible() {
    let run = mutated("p1-run-push-main.json", |run| {
        run["status"] = Value::from("waiting")
    });
    assert_eq!(
        decide(&pairs_with("probe.yml"), &key(48), &run, &reply(MERGE_PULL)),
        Ok(48)
    );
}

#[test]
fn finished_runs_are_refused() {
    let pairs = pairs_with("probe.yml");
    assert_eq!(
        decide_run(&pairs, &key(48), &reply("p1-run-push-main.json")).unwrap_err(),
        Refusal::Finished
    );

    for status in ["blocked", "failure", "cancelled", "skipped"] {
        let run = mutated("p1-run-push-main.json", |run| {
            run["status"] = Value::from(status)
        });
        assert_eq!(
            decide_run(&pairs, &key(48), &run).unwrap_err(),
            Refusal::Finished,
            "{status}"
        );
    }
}

#[test]
fn check_3_refuses_branches_and_tags() {
    let probe = pairs_with("probe.yml");
    assert_eq!(
        decide_run(&probe, &key(56), &running("p1-run-push-branch.json")).unwrap_err(),
        Refusal::NotMain
    );
    assert_eq!(
        decide_run(
            &pairs_with("test.yml"),
            &key(15),
            &running("p1-run-push-tag-main.json")
        )
        .unwrap_err(),
        Refusal::NotMain
    );

    // Section 4.4, item 3 of "What each check refuses": the tag run 65 is at
    // the merge commit, so check 4 passes it. Only check 3 refuses it.
    let tag_run = running("p1-run-push-tag.json");
    assert_eq!(
        decide_run(&probe, &key(65), &tag_run).unwrap_err(),
        Refusal::NotMain
    );
    let as_main = mutated("p1-run-push-tag.json", |run| {
        run["status"] = Value::from("running");
        run["event_payload"] = Value::from(r#"{"ref":"refs/heads/main"}"#);
    });
    assert_eq!(
        decide(&probe, &key(65), &as_main, &reply(MERGE_PULL)),
        Ok(65)
    );
}

#[test]
fn check_2_refuses_other_events() {
    let probe = pairs_with("probe.yml");
    assert_eq!(
        decide_run(&probe, &key(70), &running("p1-run-pull-request.json")).unwrap_err(),
        Refusal::NotPush
    );
    assert_eq!(
        decide_run(
            &pairs_with("prt.yml"),
            &key(69),
            &running("p11-run-prt.json")
        )
        .unwrap_err(),
        Refusal::NotPush
    );
    assert_eq!(
        decide_run(
            &pairs_with("rel.yml"),
            &key(104),
            &running("p1-run-workflow-dispatch.json")
        )
        .unwrap_err(),
        Refusal::NotPush
    );

    // A push event with another trigger is refused too.
    let other = mutated("p1-run-push-main.json", |run| {
        run["status"] = Value::from("running");
        run["trigger_event"] = Value::from("schedule");
    });
    assert_eq!(
        decide_run(&probe, &key(48), &other).unwrap_err(),
        Refusal::NotPush
    );
}

#[test]
fn check_1_refuses_other_pairs() {
    let run = running("p1-run-push-main.json");
    assert_eq!(
        decide_run(&pairs_with("release.yml"), &key(48), &run).unwrap_err(),
        Refusal::NotAPair
    );

    let other_owner =
        SigningPairs::parse("other-owner".parse().unwrap(), "runner-test\tprobe.yml\n").unwrap();
    assert_eq!(
        decide_run(&other_owner, &key(48), &run).unwrap_err(),
        Refusal::NotAPair
    );
}

#[test]
fn reply_must_match_the_key() {
    let pairs = pairs_with("probe.yml");
    let run = running("p1-run-push-main.json");

    // 23 is the `index_in_repo` of run 48 in its `html_url`.
    assert_eq!(
        decide_run(&pairs, &key(23), &run).unwrap_err(),
        Refusal::ReplyMismatch
    );

    let other_repo = RunKey {
        repository: "example-owner/creche".parse().unwrap(),
        run: 48.try_into().unwrap(),
    };
    assert_eq!(
        decide_run(&pairs, &other_repo, &run).unwrap_err(),
        Refusal::ReplyMismatch
    );

    let case = RunKey {
        repository: "Example-Owner/runner-test".parse().unwrap(),
        run: 48.try_into().unwrap(),
    };
    assert_eq!(
        decide_run(&pairs, &case, &run).unwrap_err(),
        Refusal::ReplyMismatch
    );
}

#[test]
fn check_4_refusals() {
    let pairs = pairs_with("probe.yml");
    let run = running("p1-run-push-main.json");
    let check = |pull: Reply| decide(&pairs, &key(48), &run, &pull).unwrap_err();

    assert_eq!(
        check(reply("p1-commit-pull-pr-head.json")),
        Refusal::NotAMerge(MergeRefusal::NoPull)
    );
    assert_eq!(
        check(mutated(MERGE_PULL, |pull| pull["merged"] = Value::Bool(false))),
        Refusal::NotAMerge(MergeRefusal::NotMerged)
    );
    assert_eq!(
        check(mutated(MERGE_PULL, |pull| pull["base"]["ref"] =
            Value::from("rec-release"))),
        Refusal::NotAMerge(MergeRefusal::OtherBase)
    );
    assert_eq!(
        check(mutated(MERGE_PULL, |pull| pull["merge_commit_sha"] =
            Value::from("bfe53d3a42c4526741e6890f997d52916fbc381d"))),
        Refusal::NotAMerge(MergeRefusal::OtherCommit)
    );
    assert_eq!(
        check(mutated(MERGE_PULL, |pull| pull["merge_commit_sha"] = Value::Null)),
        Refusal::NotAMerge(MergeRefusal::OtherCommit)
    );
    assert_eq!(
        check(Reply {
            status: 500,
            body: Vec::new()
        }),
        Refusal::Fault(ReplyFault::Status(500))
    );
}

#[test]
fn refusal_codes_are_stable() {
    let codes = [
        (Refusal::Fault(ReplyFault::TooLarge), "fault"),
        (Refusal::ReplyMismatch, "reply_mismatch"),
        (Refusal::NotAPair, "not_a_pair"),
        (Refusal::NotPush, "not_push"),
        (Refusal::NotMain, "not_main"),
        (Refusal::Finished, "finished"),
        (Refusal::NotAMerge(MergeRefusal::NoPull), "no_pull"),
        (Refusal::NotAMerge(MergeRefusal::NotMerged), "not_merged"),
        (Refusal::NotAMerge(MergeRefusal::OtherBase), "other_base"),
        (
            Refusal::NotAMerge(MergeRefusal::OtherCommit),
            "other_commit",
        ),
    ];
    for (refusal, code) in codes {
        assert_eq!(refusal.code(), code);
    }
}
