//! The `drop` handler on paused time with fake VMs (decisions CH21-10,
//! CH27, CH33 rules 4, 5 and 11, CH34 rule 6, CH50 rule 4). The fake stop
//! takes the time that the test sets. A temp state dir holds real rows
//! and session dirs, so a test sees each file that a drop leaves.

use std::collections::HashSet;
use std::fs::{Permissions, set_permissions};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::task::Poll;
use std::time::Duration;

use creche_vmm::handlers::acp::{Acp, StartFault, StartVm};
use creche_vmm::handlers::drop::{Drops, StopVm, VmStopFailed};
use creche_vmm::relay::{AcpLink, Relays, Taken};
use creche_vmm::stop::StopError;
use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::Rows;
use creche_wire::{
    AcpError, DropError, DropRoute, Dropped, OpenAcp, Serve, ServeUpgrade as _, SessionId,
};
use tokio::runtime::Builder;
use tokio::time::{Instant, sleep, timeout};

const A: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";
const C: &str = "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f";
const D: &str = "7e8f9a0b-1c2d-4e3f-8a4b-5c6d7e8f9a0b";

/// The fakes and the setup. It is test code, so it may unwrap and index
/// (`rust/clippy.toml`).
#[cfg(test)]
mod fake {
    use super::*;

    /// The fake VM table that the start and the stop share.
    #[derive(Debug, Default)]
    pub(super) struct Vms {
        /// The sessions whose VM runs.
        pub(super) running: HashSet<SessionId>,
        /// Each stop: the session, whether its VM ran, and when it began.
        pub(super) stops: Vec<(SessionId, bool, Instant)>,
        /// How long each stop takes.
        pub(super) wait: Duration,
        /// The session whose VM outlives SIGKILL.
        pub(super) hung: Option<SessionId>,
        /// How long each start takes before its VM runs.
        pub(super) boot: Duration,
        pub(super) starts: usize,
    }

    #[derive(Debug, Clone, Default)]
    pub(super) struct Hook(pub(super) Arc<Mutex<Vms>>);

    impl StopVm for Hook {
        async fn stop(&self, id: &SessionId) -> Result<(), VmStopFailed> {
            let wait = {
                let mut vms = self.0.lock().unwrap();
                let ran = vms.running.contains(id);
                vms.stops.push((id.clone(), ran, Instant::now()));
                vms.wait
            };
            sleep(wait).await;
            let mut vms = self.0.lock().unwrap();
            if vms.hung.as_ref() == Some(id) {
                // The VM stays in the table, so admission counts its RAM.
                let error = StopError::Hung;
                return Err(VmStopFailed { pid: 4242, error });
            }
            vms.running.remove(id);
            Ok(())
        }
    }

    impl StartVm for Hook {
        async fn start(&self, row: &creche_wire::SessionRow) -> Result<AcpLink, StartFault> {
            let boot = {
                let mut vms = self.0.lock().unwrap();
                vms.starts += 1;
                vms.boot
            };
            sleep(boot).await;
            self.0.lock().unwrap().running.insert(row.id.clone());
            // The claim stopped, so the handler answers `not_found`
            // whatever the hook returns.
            Err(StartFault::SandboxStartFailed)
        }
    }

    /// A state dir with a row and a session dir with a disk for each
    /// `(family, id)`, the drop handler, and the acp handler.
    pub(super) struct Setup {
        pub(super) hook: Hook,
        pub(super) root: StateRoot,
        pub(super) rows: Arc<Mutex<Rows>>,
        pub(super) relays: Relays,
        pub(super) drops: Drops<Hook>,
        pub(super) acp: Acp<Hook>,
    }

    impl Setup {
        pub(super) fn new(test: &str, sessions: &[(&str, &str)]) -> Self {
            let dir: PathBuf =
                std::env::temp_dir().join(format!("creche-drop-{}-{test}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            let root = StateRoot::new(dir.join("state")).unwrap();
            let mut rows = Rows::open(root.clone()).unwrap();
            for (family, id) in sessions {
                let create = format!(
                    r#"{{"created_at":1793577600000,"cwd":"/home/david","family":"{family}","id":"{id}","origin":{{"door":{{}}}}}}"#
                );
                rows.create(serde_json::from_str(&create).unwrap()).unwrap();
                let session = root.session(&id.parse().unwrap());
                std::fs::create_dir_all(session.path()).unwrap();
                std::fs::write(session.disk(), b"disk").unwrap();
            }
            let (hook, relays) = (Hook::default(), Relays::default());
            let rows = Arc::new(Mutex::new(rows));
            Self {
                drops: Drops::new(rows.clone(), relays.clone(), hook.clone()),
                acp: Acp::new(rows.clone(), relays.clone(), hook.clone()),
                relays,
                hook,
                root,
                rows,
            }
        }

        pub(super) fn has_row(&self, id: &str) -> bool {
            self.rows
                .lock()
                .unwrap()
                .get(&id.parse().unwrap())
                .is_some()
        }

        /// The names in the sessions dir and in the rows dir.
        pub(super) fn files(&self) -> Vec<String> {
            let mut names = Vec::new();
            for dir in [self.root.sessions_dir(), self.root.rows_dir()] {
                for entry in std::fs::read_dir(dir).unwrap() {
                    names.push(entry.unwrap().file_name().into_string().unwrap());
                }
            }
            names.sort();
            names
        }
    }

    pub(super) fn run<F: Future>(future: F) -> F::Output {
        Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap()
            .block_on(future)
    }

    /// Runs `first` and `second` at once and returns both outputs. The
    /// tokio feature `macros` is off, so the tests have no `join!`.
    pub(super) async fn both<F: Future, G: Future>(first: F, second: G) -> (F::Output, G::Output) {
        let (mut first, mut second) = (Box::pin(first), Box::pin(second));
        let (mut one, mut two) = (None, None);
        std::future::poll_fn(|context| {
            if one.is_none()
                && let Poll::Ready(output) = first.as_mut().poll(context)
            {
                one = Some(output);
            }
            if two.is_none()
                && let Poll::Ready(output) = second.as_mut().poll(context)
            {
                two = Some(output);
            }
            if one.is_some() && two.is_some() {
                Poll::Ready(())
            } else {
                Poll::Pending
            }
        })
        .await;
        (one.unwrap(), two.unwrap())
    }

    pub(super) fn target(json: &str) -> creche_wire::DropTarget {
        serde_json::from_str(json).unwrap()
    }

    /// The `acp` request of session `id` of `chat`.
    pub(super) fn open(id: &str) -> OpenAcp {
        serde_json::from_str(&format!(r#"{{"family":"chat","id":"{id}"}}"#)).unwrap()
    }

    pub(super) fn set_mode(path: &Path, mode: u32) {
        set_permissions(path, Permissions::from_mode(mode)).unwrap();
    }
}

use fake::{Setup, both, open, run, set_mode, target};

/// A family drop stops each VM of the family at once, within 35 s, and
/// leaves no file of the family. The other family keeps its session
/// (decisions CH21-10, CH33 rule 11).
#[test]
fn a_family_drop_stops_every_vm_at_once() {
    let setup = Setup::new(
        "family",
        &[("chat", A), ("chat", B), ("chat", C), ("code", D)],
    );
    setup.hook.0.lock().unwrap().wait = Duration::from_secs(35);
    let begun = run(async {
        let begun = Instant::now();
        let dropped =
            Serve::<DropRoute>::serve(&setup.drops, target(r#"{"family":{"family":"chat"}}"#));
        assert_eq!(dropped.await, Ok(Dropped {}));
        assert_eq!(begun.elapsed(), Duration::from_secs(35));
        begun
    });
    let vms = setup.hook.0.lock().unwrap();
    let mut stopped: Vec<String> = vms.stops.iter().map(|(id, _, _)| id.to_string()).collect();
    stopped.sort();
    assert_eq!(stopped, [B, A, C]);
    assert!(vms.stops.iter().all(|(_, _, at)| *at == begun));
    assert!(!setup.has_row(A) && setup.has_row(D));
    assert_eq!(setup.files(), [D.to_owned(), format!("{D}.json")]);
}

/// A family with no rows answers `Dropped` and stops nothing.
#[test]
fn a_family_with_no_rows_is_dropped() {
    let setup = Setup::new("empty", &[("chat", A)]);
    let dropped = run(Serve::<DropRoute>::serve(
        &setup.drops,
        target(r#"{"family":{"family":"code"}}"#),
    ));
    assert_eq!(dropped, Ok(Dropped {}));
    assert!(setup.hook.0.lock().unwrap().stops.is_empty());
    assert!(setup.has_row(A));
}

/// Another family's id gets `not_found` and stops nothing. A retry of a
/// dropped session gets `not_found` (decision CH33 rules 4 and 5).
#[test]
fn a_retry_and_another_family_get_not_found() {
    let setup = Setup::new("retry", &[("chat", A)]);
    let code = format!(r#"{{"session":{{"family":"code","id":"{A}"}}}}"#);
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    run(async {
        let drop = |json: &str| Serve::<DropRoute>::serve(&setup.drops, target(json));
        assert_eq!(drop(&code).await, Err(DropError::NotFound {}));
        assert!(setup.hook.0.lock().unwrap().stops.is_empty());
        assert_eq!(drop(&chat).await, Ok(Dropped {}));
        assert_eq!(drop(&chat).await, Err(DropError::NotFound {}));
    });
    assert!(setup.files().is_empty());
}

/// A VM that outlives SIGKILL gives `internal` and keeps its row and its
/// files. A retry after the VM ends finishes the drop (decision CH50 rule
/// 4).
#[test]
fn a_hung_vm_keeps_its_row() {
    let setup = Setup::new("hung", &[("chat", A)]);
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    setup.hook.0.lock().unwrap().hung = Some(A.parse().unwrap());
    let drop = || run(Serve::<DropRoute>::serve(&setup.drops, target(&chat)));
    assert_eq!(drop(), Err(DropError::Internal {}));
    assert!(setup.has_row(A));
    assert_eq!(setup.files(), [A.to_owned(), format!("{A}.json")]);
    setup.hook.0.lock().unwrap().hung = None;
    assert_eq!(drop(), Ok(Dropped {}));
    assert!(setup.files().is_empty());
}

/// An Upgrade during the stop of a drop gets `not_found` and starts no VM
/// (decision CH34 rule 6).
#[test]
fn an_acp_during_a_drop_gets_not_found() {
    let setup = Setup::new("acp", &[("chat", A)]);
    setup.hook.0.lock().unwrap().wait = Duration::from_secs(30);
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    let open = serde_json::from_str(&format!(r#"{{"family":"chat","id":"{A}"}}"#)).unwrap();
    let (dropped, admitted) = run(async {
        both(
            Serve::<DropRoute>::serve(&setup.drops, target(&chat)),
            async {
                sleep(Duration::from_secs(1)).await;
                setup.acp.admit(open).await.map(|_| ())
            },
        )
        .await
    });
    assert_eq!(dropped, Ok(Dropped {}));
    assert_eq!(admitted, Err(AcpError::NotFound {}));
    assert_eq!(setup.hook.0.lock().unwrap().starts, 0);
}

/// A drop that begins during a start waits for it, then stops the VM that
/// the start booted. The Upgrade gets `not_found` (decision CH34 rule 6).
#[test]
fn a_drop_stops_the_vm_of_an_overlapped_start() {
    let setup = Setup::new("overlap", &[("chat", A)]);
    setup.hook.0.lock().unwrap().boot = Duration::from_secs(5);
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    let open = serde_json::from_str(&format!(r#"{{"family":"chat","id":"{A}"}}"#)).unwrap();
    let (admitted, dropped) = run(async {
        both(async { setup.acp.admit(open).await.map(|_| ()) }, async {
            sleep(Duration::from_secs(1)).await;
            Serve::<DropRoute>::serve(&setup.drops, target(&chat)).await
        })
        .await
    });
    assert_eq!(admitted, Err(AcpError::NotFound {}));
    assert_eq!(dropped, Ok(Dropped {}));
    let vms = setup.hook.0.lock().unwrap();
    assert_eq!(vms.stops.len(), 1);
    assert!(vms.stops[0].1, "the stop came after the boot");
    assert!(vms.running.is_empty());
}

/// Two drops of one session overlap a start. The second mark carries the
/// wait for the claim, so both stops come after the boot. One drop removes
/// the row and answers `Dropped`. The other finds no row and answers
/// `not_found` (decision CH33 rule 4). An Upgrade at the end of the stops
/// gets `not_found` with no start: the marks last until the row is gone
/// (decision CH34 rule 6).
#[test]
fn two_overlapping_drops_both_wait_for_the_start() {
    let setup = Setup::new("twice", &[("chat", A)]);
    {
        let mut vms = setup.hook.0.lock().unwrap();
        vms.boot = Duration::from_secs(5);
        vms.wait = Duration::from_secs(1);
    }
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    let (drops, acp, chat) = (&setup.drops, &setup.acp, &chat);
    let drop_at = |at: u64| async move {
        sleep(Duration::from_secs(at)).await;
        Serve::<DropRoute>::serve(drops, target(chat)).await
    };
    let admit_at = |at: u64| async move {
        sleep(Duration::from_secs(at)).await;
        acp.admit(open(A)).await.map(|_| ())
    };
    let (begun, ((first, one), (two, last))) = run(async {
        let begun = Instant::now();
        let ends = both(
            both(admit_at(0), drop_at(1)),
            // The stops end at 6 s, and this Upgrade comes in that tick.
            both(drop_at(2), admit_at(6)),
        );
        (begun, ends.await)
    });
    assert_eq!(first, Err(AcpError::NotFound {}));
    assert_eq!(last, Err(AcpError::NotFound {}));
    let answers = [one, two];
    assert!(answers.contains(&Ok(Dropped {})), "{answers:?}");
    assert!(
        answers.contains(&Err(DropError::NotFound {})),
        "{answers:?}"
    );
    let vms = setup.hook.0.lock().unwrap();
    assert_eq!(vms.starts, 1);
    assert_eq!(vms.stops.len(), 2);
    let booted = begun + Duration::from_secs(5);
    assert!(
        vms.stops.iter().all(|(_, _, at)| *at >= booted),
        "{:?}",
        vms.stops
    );
    assert!(setup.files().is_empty());
}

/// The client of a drop hangs up while the drop waits for a start, and
/// hyper drops the serve future. The drop runs on in its own task with
/// its marks. So the retry joins the live slot and waits for the same
/// start, and nothing is removed while the start runs. Every stop comes
/// after the boot, and no VM runs at the end (decisions CH33 rule 4, CH34
/// rule 6).
#[test]
fn a_retry_after_a_hung_up_drop_waits_for_the_start() {
    let setup = Setup::new("hangup", &[("chat", A)]);
    {
        let mut vms = setup.hook.0.lock().unwrap();
        vms.boot = Duration::from_secs(5);
        vms.wait = Duration::from_secs(1);
    }
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    let (drops, acp, chat) = (&setup.drops, &setup.acp, &chat);
    let drop_now = || Serve::<DropRoute>::serve(drops, target(chat));
    let (begun, ((admitted, retried), during)) = run(async {
        let begun = Instant::now();
        let ends = both(
            both(async { acp.admit(open(A)).await.map(|_| ()) }, async {
                sleep(Duration::from_secs(1)).await;
                // The client hangs up at 2 s, so the serve future drops.
                let hung_up = timeout(Duration::from_secs(1), drop_now()).await;
                assert!(hung_up.is_err(), "{hung_up:?}");
                sleep(Duration::from_secs(1)).await;
                drop_now().await
            }),
            async {
                // The retry began at 3 s, and the start runs until 5 s.
                sleep(Duration::from_millis(4500)).await;
                (setup.has_row(A), setup.files())
            },
        );
        let ends = ends.await;
        // The first drop's task ends in the tick of the retry.
        sleep(Duration::from_secs(1)).await;
        (begun, ends)
    });
    assert_eq!(admitted, Err(AcpError::NotFound {}));
    assert!(
        matches!(retried, Ok(Dropped {}) | Err(DropError::NotFound {})),
        "{retried:?}"
    );
    assert_eq!(during, (true, vec![A.to_owned(), format!("{A}.json")]));
    let vms = setup.hook.0.lock().unwrap();
    assert_eq!(vms.starts, 1);
    let booted = begun + Duration::from_secs(5);
    assert!(
        !vms.stops.is_empty() && vms.stops.iter().all(|(_, ran, at)| *ran && *at >= booted),
        "{:?}",
        vms.stops
    );
    assert!(vms.running.is_empty());
    assert!(setup.files().is_empty());
}

/// A family drop with one hung VM removes the other sessions and keeps
/// only the hung one, with its row and files (decision CH50 rule 4). A
/// retry after that VM ends finishes the drop.
#[test]
fn a_family_drop_with_a_hung_vm_keeps_only_that_session() {
    let setup = Setup::new("partial", &[("chat", A), ("chat", B), ("chat", C)]);
    setup.hook.0.lock().unwrap().hung = Some(B.parse().unwrap());
    let family = || target(r#"{"family":{"family":"chat"}}"#);
    let drop = || run(Serve::<DropRoute>::serve(&setup.drops, family()));
    assert_eq!(drop(), Err(DropError::Internal {}));
    assert!(setup.has_row(B) && !setup.has_row(A) && !setup.has_row(C));
    assert_eq!(setup.files(), [B.to_owned(), format!("{B}.json")]);
    setup.hook.0.lock().unwrap().hung = None;
    assert_eq!(drop(), Ok(Dropped {}));
    assert!(setup.files().is_empty());
}

/// A removal that fails answers `internal` and keeps the row (PR D14). A
/// session dir with mode 0500 keeps its disk. A retry after the mode is
/// back finishes the drop.
#[test]
fn a_failed_removal_keeps_the_row() {
    let setup = Setup::new("removal", &[("chat", A)]);
    let session = setup.root.session(&A.parse().unwrap());
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    let drop = || run(Serve::<DropRoute>::serve(&setup.drops, target(&chat)));
    set_mode(session.path(), 0o500);
    assert_eq!(drop(), Err(DropError::Internal {}));
    assert!(setup.has_row(A));
    set_mode(session.path(), 0o700);
    assert_eq!(drop(), Ok(Dropped {}));
    assert!(setup.files().is_empty());
}

/// The marks last until the row is gone. While the removal waits for the
/// lock of the rows, a claim of the session is refused (decision CH34
/// rule 6). The test runs on real time: paused time does not advance
/// while the removal blocks in `spawn_blocking`.
#[test]
fn the_marks_last_until_the_row_is_gone() {
    let setup = Setup::new("marks", &[("chat", A)]);
    setup.hook.0.lock().unwrap().wait = Duration::from_millis(50);
    let chat = format!(r#"{{"session":{{"family":"chat","id":"{A}"}}}}"#);
    let (lock, locked, free) = (mpsc::channel(), mpsc::channel(), mpsc::channel());
    let rows = Arc::clone(&setup.rows);
    let holder = std::thread::spawn(move || {
        lock.1.recv().unwrap();
        let _rows = rows.lock().unwrap();
        locked.0.send(()).unwrap();
        free.1.recv().unwrap();
    });
    let runtime = Builder::new_current_thread().enable_time().build().unwrap();
    let (dropped, claimed) = runtime.block_on(both(
        Serve::<DropRoute>::serve(&setup.drops, target(&chat)),
        async {
            // The drop read its rows and waits for its stop.
            sleep(Duration::from_millis(10)).await;
            lock.0.send(()).unwrap();
            locked.1.recv().unwrap();
            // The stop ended at 50 ms, and the removal waits for the rows.
            sleep(Duration::from_millis(100)).await;
            let claimed = setup.relays.claim(&A.parse().unwrap()).map(|_| ());
            free.0.send(()).unwrap();
            claimed
        },
    ));
    holder.join().unwrap();
    assert_eq!(claimed, Err(Taken::Dropping));
    assert_eq!(dropped, Ok(Dropped {}));
    assert!(setup.files().is_empty());
}
