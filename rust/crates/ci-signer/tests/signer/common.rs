//! Readers of the recordings. Each reply takes its body from the file and
//! its status from `index.tsv`, so no status is a literal of a test.

use std::path::PathBuf;

use creche_ci_signer::eligibility::RunKey;
use creche_ci_signer::forgejo::Reply;
use creche_ci_signer::pairs::SigningPairs;
use serde_json::Value;

/// The owner of every sanitized recording.
pub const OWNER: &str = "example-owner";

/// The repository of every run recording.
pub const REPOSITORY: &str = "example-owner/runner-test";

/// The merge commit of the recorded PR 2, which run 48 built.
pub const MERGE_COMMIT: &str = "5791a8cdae55087207f12ab919c5eb964494d654";

pub fn crate_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

pub fn fixture_dir() -> PathBuf {
    crate_dir().join("tests/fixtures/forgejo-17.0.1")
}

/// The fields of the `index.tsv` row of `name`: file, method, path,
/// status, token.
pub fn index_row(name: &str) -> Vec<String> {
    let index = std::fs::read_to_string(fixture_dir().join("index.tsv")).unwrap();
    let row = index
        .lines()
        .find(|line| line.split('\t').next() == Some(name))
        .unwrap_or_else(|| panic!("{name} has no row in index.tsv"));
    row.split('\t').map(str::to_owned).collect()
}

/// The recorded reply of `name`.
pub fn reply(name: &str) -> Reply {
    let status = index_row(name)[3].parse().unwrap();
    let body = std::fs::read(fixture_dir().join(name)).unwrap();
    Reply { status, body }
}

/// The recorded body of `name` as JSON.
pub fn json(name: &str) -> Value {
    serde_json::from_slice(&reply(name).body).unwrap()
}

/// The recorded reply of `name` with a change made in the test. The status
/// stays the recorded one.
pub fn mutated(name: &str, change: impl FnOnce(&mut Value)) -> Reply {
    let mut value = json(name);
    change(&mut value);
    Reply {
        status: reply(name).status,
        body: serde_json::to_vec(&value).unwrap(),
    }
}

/// A run reply whose status is `running`. Every recorded run is `success`,
/// so each test of an unfinished run changes the status in test code.
pub fn running(name: &str) -> Reply {
    mutated(name, |run| run["status"] = Value::from("running"))
}

/// The pairs that make the recorded repository a signing pair with
/// `workflow`, so each test isolates one check.
pub fn pairs_with(workflow: &str) -> SigningPairs {
    SigningPairs::parse(
        OWNER.parse().unwrap(),
        &format!("runner-test\t{workflow}\n"),
    )
    .unwrap()
}

/// The key of the open line for `run` of the recorded repository.
pub fn key(run: u64) -> RunKey {
    RunKey {
        repository: REPOSITORY.parse().unwrap(),
        run: run.try_into().unwrap(),
    }
}

/// The `message` field of a recorded tag reply.
pub fn message_of(name: &str) -> String {
    json(name)["message"].as_str().unwrap().to_owned()
}
