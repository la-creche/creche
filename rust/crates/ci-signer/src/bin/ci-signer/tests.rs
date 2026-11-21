//! The tests of the program: a stand-in Forgejo that serves the recorded
//! replies of Forgejo 17.0.1, a stand-in cosign, and a stand-in daemon on a
//! socketpair (Linux only).
//!
//! Each run reply is a recording of `tests/fixtures/forgejo-17.0.1/`. The
//! recordings come from the repository `runner-test`, and the signer takes
//! its pairs from `signing-pairs.tsv`. So a test that isolates one check
//! changes `repository.full_name` to `example-owner/creche` and
//! `workflow_id` to `release.yml` in test code, as the tests of the core
//! change the status. Each recorded run is `success`, so a test of an
//! unfinished run changes the status to `running`.
//!
//! The kernel checks of `ci-runner-peer` pass only for a real slot unit of
//! a runner host and for the user `ci-runner`. So the session tests start
//! at `session::serve`, after the daemon check, and each passed fd is
//! refused by the peer check: the test process runs in no slot unit. The
//! guest exchange that follows an accepted fd is tested on its own, from
//! an `Eligible` run of the recordings.

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use creche_ci_signer::eligibility::{Eligible, RunKey, decide_merge, decide_run};
use creche_ci_signer::forgejo::Reply;
use creche_ci_signer::pairs::SigningPairs;
use creche_ci_signer::statement::Statement;
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UnixStream};

use crate::config::Config;
use crate::guest;
use crate::signer::{Paths, Signer};

/// The owner of the recordings.
const OWNER: &str = "example-owner";

/// The repository of the signing pairs that each isolated case names.
const REPOSITORY: &str = "example-owner/creche";

/// The merge commit of run 48 of the recordings.
const MERGE_COMMIT: &str = "5791a8cdae55087207f12ab919c5eb964494d654";

/// The token of `release-reader` and of the bot `ci-signer` in the tests.
const READER: &str = "1111111111111111111111111111111111111111";
const TAGGER: &str = "2222222222222222222222222222222222222222";

/// One request that the stand-in Forgejo got.
#[derive(Debug, Clone)]
pub(crate) struct Seen {
    pub(crate) method: String,
    pub(crate) path: String,
    pub(crate) authorization: String,
    pub(crate) body: Vec<u8>,
}

/// The replies of the stand-in by method and path, and what it got.
#[derive(Debug, Default)]
pub(crate) struct Forgejo {
    replies: BTreeMap<(String, String), Reply>,
    pub(crate) seen: Mutex<Vec<Seen>>,
}

fn fixture(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/forgejo-17.0.1")
        .join(name);
    fs::read(path).unwrap()
}

/// The status of a recording, from `index.tsv`.
fn status(name: &str) -> u16 {
    let index = String::from_utf8(fixture("index.tsv")).unwrap();
    let row = index
        .lines()
        .find(|line| line.split('\t').next() == Some(name))
        .unwrap();
    row.split('\t').nth(3).unwrap().parse().unwrap()
}

/// The recorded reply `name` with `change` made in test code.
pub(crate) fn recorded(name: &str, change: impl FnOnce(&mut Value)) -> Reply {
    let mut value: Value = serde_json::from_slice(&fixture(name)).unwrap();
    change(&mut value);
    Reply {
        status: status(name),
        body: serde_json::to_vec(&value).unwrap(),
    }
}

/// A recorded run made a run of the release workflow of creche.
pub(crate) fn as_release(run: &mut Value) {
    run["repository"]["full_name"] = Value::from(REPOSITORY);
    run["workflow_id"] = Value::from("release.yml");
}

/// `as_release`, and the status `running`.
pub(crate) fn running_release(run: &mut Value) {
    as_release(run);
    run["status"] = Value::from("running");
}

pub(crate) fn key(run: u64) -> RunKey {
    RunKey {
        repository: REPOSITORY.parse().unwrap(),
        run: run.try_into().unwrap(),
    }
}

fn run_path(run: u64) -> String {
    format!("/api/v1/repos/{REPOSITORY}/actions/runs/{run}")
}

fn pull_path() -> String {
    format!("/api/v1/repos/{REPOSITORY}/commits/{MERGE_COMMIT}/pull")
}

impl Forgejo {
    /// The stand-in with the run `run` as `reply` and the recorded PR of
    /// the merge commit.
    pub(crate) fn with_run(run: u64, reply: Reply) -> Self {
        let mut forgejo = Self::default();
        forgejo.get(&run_path(run), reply);
        forgejo.get(
            &pull_path(),
            recorded("p1-commit-pull-merge-sha.json", |_| {}),
        );
        forgejo
    }

    pub(crate) fn get(&mut self, path: &str, reply: Reply) {
        self.replies
            .insert(("GET".to_owned(), path.to_owned()), reply);
    }

    pub(crate) fn posts(&self) -> Vec<Seen> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .filter(|seen| seen.method == "POST")
            .cloned()
            .collect()
    }

    /// The reply to one request. A `POST .../tags` answers 201 with the
    /// recorded create reply, its name, commit and message those of the
    /// request.
    fn answer(&self, seen: &Seen) -> Reply {
        if seen.method == "POST" && seen.path.ends_with("/tags") {
            let request: Value = serde_json::from_slice(&seen.body).unwrap();
            return recorded("p2-tag-create.json", |tag| {
                tag["name"] = request["tag_name"].clone();
                tag["message"] = request["message"].clone();
                tag["commit"]["sha"] = request["target"].clone();
            });
        }
        self.replies
            .get(&(seen.method.clone(), seen.path.clone()))
            .cloned()
            .unwrap_or(Reply {
                status: 404,
                body: b"{}".to_vec(),
            })
    }

    /// Serves on a loopback port and gives the origin.
    pub(crate) async fn start(self: &Arc<Self>) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let forgejo = Arc::clone(self);
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let forgejo = Arc::clone(&forgejo);
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<Incoming>| {
                        let forgejo = Arc::clone(&forgejo);
                        async move {
                            let method = request.method().to_string();
                            let path = request.uri().path().to_owned();
                            let authorization = request
                                .headers()
                                .get("authorization")
                                .map(|value| value.to_str().unwrap().to_owned())
                                .unwrap_or_default();
                            let body = request.into_body().collect().await?.to_bytes().to_vec();
                            let seen = Seen {
                                method,
                                path,
                                authorization,
                                body,
                            };
                            let reply = forgejo.answer(&seen);
                            forgejo.seen.lock().unwrap().push(seen);
                            let response = Response::builder()
                                .status(reply.status)
                                .body(Full::new(Bytes::from(reply.body)))
                                .unwrap();
                            Ok::<_, hyper::Error>(response)
                        }
                    });
                    let _ended = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), service)
                        .await;
                });
            }
        });
        format!("http://{address}")
    }
}

/// The stand-in cosign: it drains stdin, requires an empty
/// `COSIGN_PASSWORD`, and writes a bundle at the path of `--bundle`. It
/// uses builtins of `sh` only, because the child has no `PATH`.
const STAND_IN_COSIGN: &str = r#"#!/bin/sh
bundle=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--bundle" ]; then bundle="$2"; shift; fi
  shift
done
while IFS= read -r line; do :; done
[ "${COSIGN_PASSWORD-unset}" = "" ] || exit 3
printf '%s' '{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json","standIn":true}' > "$bundle"
"#;

/// A stand-in cosign that fails.
const FAILING_COSIGN: &str = "#!/bin/sh\nexit 1\n";

/// The folders of one test, removed at its drop.
pub(crate) struct Host {
    pub(crate) root: PathBuf,
    pub(crate) paths: Paths,
}

impl Drop for Host {
    fn drop(&mut self) {
        let _gone = fs::remove_dir_all(&self.root);
    }
}

static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

impl Host {
    /// A new host with the credentials and the stand-in `cosign`.
    pub(crate) fn new(cosign: &str) -> Self {
        let number = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("ci-signer-test-{}-{number}", std::process::id()));
        let _gone = fs::remove_dir_all(&root);
        let credentials = root.join("credentials");
        let work = root.join("work");
        fs::create_dir_all(&credentials).unwrap();
        fs::create_dir_all(&work).unwrap();
        fs::write(credentials.join("release-signing.key"), b"stand-in key\n").unwrap();
        fs::write(
            credentials.join("release-reader.token"),
            format!("{READER}\n"),
        )
        .unwrap();
        fs::write(credentials.join("ci-signer.token"), format!("{TAGGER}\n")).unwrap();
        let program = root.join("cosign");
        fs::write(&program, cosign).unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
        let paths = Paths {
            journal: root.join("issued.tsv"),
            work,
            credentials,
        };
        Self { root, paths }
    }

    pub(crate) fn config(&self, origin: &str) -> Config {
        let text = format!(
            "owner={OWNER}\nforgejo={origin}\ncosign={}\n",
            self.root.join("cosign").display()
        );
        Config::parse(text.as_bytes()).unwrap()
    }

    pub(crate) fn journal(&self) -> String {
        fs::read_to_string(&self.paths.journal).unwrap_or_default()
    }
}

/// A runtime of real time for the tests that talk to the stand-in.
pub(crate) fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}

/// A runtime of paused time for the tests of a bound. No stand-in Forgejo
/// runs in it: an idle runtime moves the clock, so each wait of a call
/// would pass at once.
pub(crate) fn paused() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .start_paused(true)
        .build()
        .unwrap()
}

/// The eligible run 48 of the recordings, with no read of Forgejo.
fn eligible_48() -> Eligible {
    let pairs = SigningPairs::builtin(OWNER.parse().unwrap()).unwrap();
    let run = recorded("p1-run-push-main.json", running_release);
    let checked = decide_run(&pairs, &key(48), &run).unwrap();
    decide_merge(checked, &recorded("p1-commit-pull-merge-sha.json", |_| {})).unwrap()
}

/// The request line of two tags: one with an asset, one with none.
const REQUEST: &str = "{\"v\":1,\"tags\":[{\"tag\":\"vmm-v0.1.24\",\"assets\":[{\"name\":\"vmm-x86_64-unknown-linux-musl.tar.gz\",\"sha256\":\"0000000000000000000000000000000000000000000000000000000000000000\"}]},{\"tag\":\"handover-v0.1.96\",\"assets\":[]}]}\n";

/// Sends `request` on a fresh guest connection of `held`, and gives the
/// answer line.
async fn exchange(signer: &Arc<Signer>, held: Option<Eligible>, request: &[u8]) -> Value {
    let (ours, theirs) = UnixStream::pair().unwrap();
    let task = tokio::spawn(guest::serve(theirs, held, Arc::clone(signer)));
    let mut ours = ours;
    ours.write_all(request).await.unwrap();
    let mut answer = Vec::new();
    ours.read_to_end(&mut answer).await.unwrap();
    task.await.unwrap();
    assert_eq!(answer.last(), Some(&b'\n'), "{answer:?}");
    serde_json::from_slice(&answer).unwrap()
}

#[test]
fn the_eligible_run_gets_its_statement_its_journal_line_and_its_tags() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let forgejo = Arc::new(Forgejo::with_run(
            48,
            recorded("p1-run-push-main.json", running_release),
        ));
        let origin = forgejo.start().await;
        let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());

        let eligible = signer.decide(&key(48)).await.unwrap();
        assert_eq!(eligible.commit().as_str(), MERGE_COMMIT);
        let answer = exchange(&signer, Some(eligible), REQUEST.as_bytes()).await;
        assert_eq!(answer["v"], 1, "{answer}");

        // The statement takes its facts from Forgejo, the tags from the
        // request, sorted by name (FR7, FR25).
        let statement =
            creche_ci_signer::base64_decode(answer["signed"]["statement"].as_str().unwrap())
                .unwrap();
        let statement = Statement::from_canonical(&statement).unwrap();
        assert_eq!(statement.repository().to_string(), REPOSITORY);
        assert_eq!(statement.workflow().as_str(), "release.yml");
        assert_eq!(statement.commit().as_str(), MERGE_COMMIT);
        assert_eq!(statement.run().get(), 48);
        let tags: Vec<&str> = statement
            .tags()
            .items()
            .iter()
            .map(|entry| entry.tag.as_str())
            .collect();
        assert_eq!(tags, ["handover-v0.1.96", "vmm-v0.1.24"]);

        // The journal holds one line of run 48 at the merge commit (FR21).
        let journal = host.journal();
        assert_eq!(journal.lines().count(), 1, "{journal}");
        assert!(
            journal.starts_with(&format!("48\t{MERGE_COMMIT}\t")),
            "{journal}"
        );

        // The two reads used the token of release-reader. Each tag is one
        // POST with the token of the bot ci-signer, at the merge commit,
        // with the one message of the statement (section 4.5 step 7).
        let seen = forgejo.seen.lock().unwrap().clone();
        let reads: Vec<&Seen> = seen.iter().filter(|one| one.method == "GET").collect();
        assert_eq!(reads.len(), 2);
        assert!(
            reads
                .iter()
                .all(|one| one.authorization == format!("token {READER}"))
        );
        let posts = forgejo.posts();
        assert_eq!(posts.len(), 2);
        let mut messages = Vec::new();
        for post in &posts {
            assert_eq!(post.path, format!("/api/v1/repos/{REPOSITORY}/tags"));
            assert_eq!(post.authorization, format!("token {TAGGER}"));
            let body: Value = serde_json::from_slice(&post.body).unwrap();
            assert_eq!(body["target"], MERGE_COMMIT);
            messages.push(body["message"].as_str().unwrap().to_owned());
        }
        assert_eq!(messages[0], messages[1]);
        assert!(messages[0].starts_with("release-statement-1\n"));

        // A second request of the same session has no eligible run left.
        let again = exchange(&signer, None, REQUEST.as_bytes()).await;
        assert_eq!(
            again,
            serde_json::json!({"v": 1, "refused": "already_issued"})
        );

        // A second open of the run is refused: the journal holds it (FR22).
        let decline = signer.decide(&key(48)).await.unwrap_err();
        assert_eq!(decline.code(), "already_issued");
    });
}

/// Each failed check of section 4.4 on the recorded replies gives no
/// statement: `decide` refuses, and no POST and no journal line follow.
#[test]
fn each_failed_check_gives_no_statement() {
    let cases: Vec<(u64, Reply, Option<Reply>, &str)> = vec![
        // The reply names another repository than the key of the open line.
        (
            48,
            recorded("p1-run-push-main.json", |run| {
                run["status"] = Value::from("running")
            }),
            None,
            "reply_mismatch",
        ),
        // Check 1: the workflow of the run is not the one of the pair.
        (
            48,
            recorded("p1-run-push-main.json", |run| {
                running_release(run);
                run["workflow_id"] = Value::from("probe.yml");
            }),
            None,
            "not_a_pair",
        ),
        // Check 2: a PR, a pull_request_target and a workflow_dispatch.
        (
            70,
            recorded("p1-run-pull-request.json", running_release),
            None,
            "not_push",
        ),
        (
            69,
            recorded("p11-run-prt.json", running_release),
            None,
            "not_push",
        ),
        (
            104,
            recorded("p1-run-workflow-dispatch.json", running_release),
            None,
            "not_push",
        ),
        // Check 3: a branch, a tag, and a tag named main.
        (
            56,
            recorded("p1-run-push-branch.json", running_release),
            None,
            "not_main",
        ),
        (
            65,
            recorded("p1-run-push-tag.json", running_release),
            None,
            "not_main",
        ),
        (
            15,
            recorded("p1-run-push-tag-main.json", running_release),
            None,
            "not_main",
        ),
        // Check 5: the recorded run is over.
        (
            48,
            recorded("p1-run-push-main.json", as_release),
            None,
            "finished",
        ),
        // Check 4: the commit is no PR merge (the recorded 404).
        (
            48,
            recorded("p1-run-push-main.json", running_release),
            Some(recorded("p1-commit-pull-pr-head.json", |_| {})),
            "no_pull",
        ),
        (
            48,
            recorded("p1-run-push-main.json", running_release),
            Some(recorded("p1-commit-pull-merge-sha.json", |pull| {
                pull["merged"] = Value::from(false)
            })),
            "not_merged",
        ),
        (
            48,
            recorded("p1-run-push-main.json", running_release),
            Some(recorded("p1-commit-pull-merge-sha.json", |pull| {
                pull["base"]["ref"] = Value::from("rec-release")
            })),
            "other_base",
        ),
        // A reply that is no run.
        (
            48,
            Reply {
                status: 404,
                body: b"{}".to_vec(),
            },
            None,
            "fault",
        ),
    ];
    runtime().block_on(async {
        for (run, reply, pull, code) in cases {
            let host = Host::new(STAND_IN_COSIGN);
            let mut forgejo = Forgejo::with_run(run, reply);
            if let Some(pull) = pull {
                forgejo.get(&pull_path(), pull);
            }
            let forgejo = Arc::new(forgejo);
            let origin = forgejo.start().await;
            let signer = Signer::start(host.config(&origin), &host.paths).unwrap();
            let decline = signer.decide(&key(run)).await.unwrap_err();
            assert_eq!(decline.code(), code, "run {run}");
            assert!(forgejo.posts().is_empty(), "{code}");
            assert_eq!(host.journal(), "", "{code}");
        }
    });
}

/// A failed sign leaves the run issued: no tag, the answer `fault`, and a
/// second open of the run is refused (fail closed).
#[test]
fn a_failed_sign_keeps_the_run_issued_and_makes_no_tag() {
    runtime().block_on(async {
        let host = Host::new(FAILING_COSIGN);
        let forgejo = Arc::new(Forgejo::with_run(
            48,
            recorded("p1-run-push-main.json", running_release),
        ));
        let origin = forgejo.start().await;
        let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
        let eligible = signer.decide(&key(48)).await.unwrap();
        let answer = exchange(&signer, Some(eligible), REQUEST.as_bytes()).await;
        assert_eq!(answer, serde_json::json!({"v": 1, "refused": "fault"}));
        assert!(forgejo.posts().is_empty());
        assert_eq!(host.journal().lines().count(), 1);
        assert_eq!(
            signer.decide(&key(48)).await.unwrap_err().code(),
            "already_issued"
        );
        // The bundle file is gone.
        assert_eq!(fs::read_dir(&host.paths.work).unwrap().count(), 2);
    });
}

/// A request that breaks the grammar or a limit gets its refusal and no
/// statement. The bytes of the guest are hostile.
#[test]
fn a_bad_request_gets_its_refusal_and_no_statement() {
    let cases: [(&[u8], &str); 5] = [
        (b"{\"v\":1,\"tags\":[]}\n", "limits"),
        (b"{\"v\":2,\"tags\":[]}\n", "grammar"),
        (b"{\"v\":1,\"tags\":[],\"run\":48}\n", "grammar"),
        (b"not json\n", "grammar"),
        (b"{\"v\":1,\"tags\":[]}\nmore", "grammar"),
    ];
    runtime().block_on(async {
        for (request, word) in cases {
            let host = Host::new(STAND_IN_COSIGN);
            let forgejo = Arc::new(Forgejo::default());
            let origin = forgejo.start().await;
            let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
            let answer = exchange(&signer, Some(eligible_48()), request).await;
            assert_eq!(
                answer,
                serde_json::json!({"v": 1, "refused": word}),
                "{word}"
            );
            assert_eq!(host.journal(), "");
            assert!(forgejo.seen.lock().unwrap().is_empty());
        }
    });
}

/// The bound of the request: a guest that sends nothing gets `grammar`
/// after `REQUEST_WAIT`, and no statement.
#[test]
fn a_guest_that_sends_nothing_is_refused_at_the_bound() {
    paused().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let config = host.config("http://127.0.0.1:9");
        let signer = Arc::new(Signer::start(config, &host.paths).unwrap());
        let (mut ours, theirs) = UnixStream::pair().unwrap();
        let started = tokio::time::Instant::now();
        let task = tokio::spawn(guest::serve(
            theirs,
            Some(eligible_48()),
            Arc::clone(&signer),
        ));
        let mut answer = Vec::new();
        ours.read_to_end(&mut answer).await.unwrap();
        task.await.unwrap();
        assert!(started.elapsed() >= guest::REQUEST_WAIT);
        assert!(started.elapsed() < guest::REQUEST_WAIT + Duration::from_secs(1));
        assert_eq!(answer, b"{\"v\":1,\"refused\":\"grammar\"}\n");
        assert_eq!(host.journal(), "");
    });
}

/// A start with a missing credential or a bad journal refuses (fail
/// closed).
#[test]
fn a_start_with_no_credential_or_a_bad_journal_is_refused() {
    let host = Host::new(STAND_IN_COSIGN);
    fs::remove_file(host.paths.credentials.join("ci-signer.token")).unwrap();
    let fault = Signer::start(host.config("http://127.0.0.1:9"), &host.paths).unwrap_err();
    assert!(fault.to_string().contains("ci-signer.token"), "{fault}");

    let host = Host::new(STAND_IN_COSIGN);
    fs::write(
        host.paths.credentials.join("release-reader.token"),
        b"short\n",
    )
    .unwrap();
    assert!(Signer::start(host.config("http://127.0.0.1:9"), &host.paths).is_err());

    let host = Host::new(STAND_IN_COSIGN);
    fs::write(&host.paths.journal, b"48\tnot-a-commit\tx\n").unwrap();
    let fault = Signer::start(host.config("http://127.0.0.1:9"), &host.paths).unwrap_err();
    assert!(fault.to_string().contains("journal"), "{fault}");

    let host = Host::new(STAND_IN_COSIGN);
    fs::write(
        &host.paths.journal,
        format!("48\t{MERGE_COMMIT}\t{}", "0".repeat(64)),
    )
    .unwrap();
    assert!(Signer::start(host.config("http://127.0.0.1:9"), &host.paths).is_err());
}

/// The real cosign, when the machine has one: the bundle of the signer
/// verifies with the public key and no log entry (section 5, M7 fact 1).
#[test]
fn the_bundle_of_the_real_cosign_verifies() {
    let Some(cosign) = [
        "/usr/bin/cosign",
        "/usr/local/bin/cosign",
        "/opt/homebrew/bin/cosign",
    ]
    .into_iter()
    .map(PathBuf::from)
    .find(|path| path.is_file()) else {
        println!("real cosign: not run: no cosign on this machine");
        return;
    };
    let host = Host::new(STAND_IN_COSIGN);
    let made = std::process::Command::new(&cosign)
        .arg("generate-key-pair")
        .env("COSIGN_PASSWORD", "")
        .current_dir(&host.paths.credentials)
        .output()
        .unwrap();
    assert!(made.status.success(), "{made:?}");
    let key = host.paths.credentials.join("cosign.key");
    let signer =
        crate::cosign::Cosign::prepare(cosign.clone(), key, host.paths.work.clone()).unwrap();

    let mut ledger = crate::ledger::Ledger::open(&host.paths.journal).unwrap();
    let request =
        creche_ci_signer::exchange::Request::parse(REQUEST.trim_end().as_bytes()).unwrap();
    let signed_at =
        creche_ci_signer::signed_at::SignedAt::from_unix_seconds(1_800_000_000).unwrap();
    let signable = ledger
        .issue(eligible_48(), request, signed_at)
        .map_err(|_| "issue")
        .unwrap();
    let bundle = runtime()
        .block_on(signer.sign(&signable, 48.try_into().unwrap()))
        .unwrap();

    let statement = host.root.join("statement.json");
    let bundle_path = host.root.join("statement.sigstore.json");
    fs::write(&statement, signable.bytes()).unwrap();
    fs::write(&bundle_path, bundle.bytes()).unwrap();
    let verified = std::process::Command::new(&cosign)
        .arg("verify-blob")
        .arg("--key")
        .arg(host.paths.credentials.join("cosign.pub"))
        .arg("--bundle")
        .arg(&bundle_path)
        .arg("--insecure-ignore-tlog")
        .arg(&statement)
        .output()
        .unwrap();
    assert!(verified.status.success(), "{verified:?}");
    println!(
        "real cosign: the bundle of {} bytes verifies",
        bundle.bytes().len()
    );
}

#[cfg(target_os = "linux")]
mod linux;
