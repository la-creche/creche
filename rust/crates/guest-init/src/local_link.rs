//! The local link: the Unix socket between guest init and the facade
//! (decisions CH21, CH32 rules 4, 6 and 7, CH62 rules 10 and 11).
//!
//! Node cannot dial vsock (fact 31), so the facade reaches guest init
//! here, and guest init relays its turn events on control link 1200.
//!
//! 1. Guest init listens on `creche_wire::guest::LOCAL_LINK_SOCKET`, mode
//!    0600 (decision CH62 rule 10). It accepts a peer only when its
//!    `SO_PEERCRED` pid is the live facade, and only the first link of
//!    each facade start (gap Q54-10 option A: one link at a time). It
//!    closes each other peer at once.
//! 2. The open moves the facade from `starting` to `running` (decision
//!    CH32 rule 6).
//! 3. The framing is the one of control link 1200 (decision CH36 G11):
//!    one compact JSON value per LF line, at most `CONTROL_LINE_CAP`
//!    bytes. Each facade line is a `TurnEvent`. Guest init reads it as it
//!    reads a line of 1200: `creche_wire::strict` first, then the typed
//!    read (decisions CH62 rule 11, CH65 rule 2).
//! 4. Guest init keeps the last `TurnEnd` for each later ready (decision
//!    CH32 rule 4). It relays each event to the current control link as
//!    `GuestEvent::Turn` once that link got its ready (decision CH21). With
//!    no such link, it drops the event (decision CH62 rule 11).
//! 5. A refused line closes the local link. A close of the local link
//!    kills the facade with SIGKILL, and the restart of the supervisor
//!    follows (decision CH32 rule 7).
//! 6. Guest init writes one line to the facade: `{"stop":{}}`, the first
//!    step of the stop (decision CH62 rule 9).
//!
//! The state lives in `control::Control` and `supervisor::Supervisor`,
//! so one thread owns it. This module holds the identity of a link, the
//! stop line and the Linux listener.

use std::fmt;

/// The identity of one local link. Each accepted peer gets a new one, in
/// accept order, as `control::LinkId` does for control link 1200.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LocalId(pub(crate) u64);

impl fmt::Display for LocalId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "local link {}", self.0)
    }
}

/// The one line that guest init writes on the local link, its LF
/// included: `ControlCommand::Stop` (decision CH62 rules 9 and 10). A
/// test holds it equal to the encoded command.
pub const STOP_LINE: &[u8] = b"{\"stop\":{}}\n";

#[cfg(target_os = "linux")]
pub use shell::{listen, peer};

/// The Unix listener and the peer check (decision CH62 rules 2 and 10).
/// The kvm tests prove it.
#[cfg(target_os = "linux")]
mod shell {
    use std::fs::{self, Permissions};
    use std::io;
    use std::num::NonZeroU32;
    use std::os::unix::fs::PermissionsExt as _;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::Path;

    use creche_wire::guest::LOCAL_LINK_SOCKET;
    use rustix::net::sockopt::socket_peercred;
    use rustix::net::{AddressFamily, SocketAddrUnix, SocketFlags, SocketType, bind, socket_with};

    /// The mode of the socket (decision CH62 rule 10).
    const MODE: u32 = 0o600;

    /// The listen backlog. One facade opens one link (decision CH62 rule
    /// 10), so a few slots are room.
    const BACKLOG: i32 = 4;

    /// Listens on `LOCAL_LINK_SOCKET`, mode 0600 (decision CH62 rule 10).
    /// The mode is set before `listen`, so no connect can come before it.
    ///
    /// # Errors
    ///
    /// The error of the dir, `socket`, `bind`, the mode or `listen`.
    // CONTRACT-QUESTION: no row decides the mode of `/run/creche`. The
    // reading taken: the dir gets the umask default of PID 1, 0755, as
    // each dir of `/run`. The socket mode 0600 guards the link. A change
    // costs one `DirBuilder` mode here.
    pub fn listen() -> io::Result<UnixListener> {
        let path = Path::new(LOCAL_LINK_SOCKET);
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let listener = socket_with(
            AddressFamily::UNIX,
            SocketType::STREAM,
            SocketFlags::CLOEXEC,
            None,
        )?;
        bind(&listener, &SocketAddrUnix::new(path)?)?;
        fs::set_permissions(path, Permissions::from_mode(MODE))?;
        rustix::net::listen(&listener, BACKLOG)?;
        Ok(UnixListener::from(listener))
    }

    /// The pid of the peer of `stream`: the process that connected
    /// (`SO_PEERCRED`, decision CH62 rule 10).
    ///
    /// # Errors
    ///
    /// The error of `getsockopt`.
    pub fn peer(stream: &UnixStream) -> io::Result<NonZeroU32> {
        let cred = socket_peercred(stream)?;
        u32::try_from(cred.pid.as_raw_nonzero().get())
            .ok()
            .and_then(NonZeroU32::new)
            .ok_or_else(|| io::Error::other("the peer pid is not positive"))
    }
}

#[cfg(test)]
mod tests {
    use std::num::NonZeroU32;

    use creche_wire::{
        ControlCommand, ControlReady, ControlStop, FacadeRunning, FacadeState, GuestEvent,
        StartCount, TurnEnd, TurnEvent, TurnStart, UnixMillis,
    };
    use serde_json::json;

    use super::{LocalId, STOP_LINE};
    use crate::control::{Action, Control, LinkId, Why};
    use crate::supervisor::RESTART_PAUSE;

    const SESSION: &str = "0123abcd-0000-4000-8000-00000000c7c7";

    /// The pid of the first facade.
    const FACADE: u32 = 7;

    fn pid(raw: u32) -> NonZeroU32 {
        NonZeroU32::new(raw).unwrap()
    }

    fn end() -> TurnEnd {
        TurnEnd {
            title: "a turn".parse().unwrap(),
            updated_at: UnixMillis::try_from(1_893_456_000_000).unwrap(),
        }
    }

    fn line<T: serde::Serialize>(value: &T) -> Vec<u8> {
        let mut line = serde_json::to_vec(value).unwrap();
        line.push(b'\n');
        line
    }

    fn turn_line(event: TurnEvent) -> Vec<u8> {
        line(&GuestEvent::Turn(event))
    }

    fn running(turn_running: bool, last_turn_end: Option<TurnEnd>) -> FacadeState {
        FacadeState::Running(FacadeRunning {
            last_turn_end,
            pid: pid(FACADE),
            starts: StartCount::try_from(1).unwrap(),
            turn_running,
        })
    }

    fn facade_line(state: FacadeState) -> Vec<u8> {
        line(&GuestEvent::Facade(state))
    }

    /// A new control link through its hello and clock set. It returns the
    /// link and the actions of the clock set.
    fn hello(control: &mut Control) -> (LinkId, Option<Action>) {
        let (link, _) = control.accept();
        let hello = json!({"sent_at": 1_893_456_000_000_u64, "session": SESSION});
        control.line(link, &serde_json::to_vec(&hello).unwrap());
        let action = control.clock_set(link, Ok(()));
        (link, action)
    }

    /// A control link that got its ready, with a facade that runs as
    /// `FACADE` and has no local link yet.
    fn ready(control: &mut Control) -> LinkId {
        let (link, action) = hello(control);
        assert_eq!(action, Some(Action::Start(SESSION.parse().unwrap())));
        control.started(pid(FACADE));
        link
    }

    /// The local link of the facade, open.
    fn open(control: &mut Control) -> LocalId {
        let (local, actions) = control.local_accept(pid(FACADE));
        assert!(
            !actions
                .iter()
                .any(|action| matches!(action, Action::LocalClose(..))),
            "the facade link is refused: {actions:?}"
        );
        local
    }

    fn start() -> Vec<u8> {
        serde_json::to_vec(&TurnEvent::Start(TurnStart {})).unwrap()
    }

    fn end_line() -> Vec<u8> {
        serde_json::to_vec(&TurnEvent::End(end())).unwrap()
    }

    #[test]
    fn the_stop_line_is_the_stop_command() {
        assert_eq!(STOP_LINE, line(&ControlCommand::Stop(ControlStop {})));
    }

    /// The open of the facade's link moves `starting` to `running`, and
    /// the link after the ready gets the change (decision CH32 rule 6).
    #[test]
    fn the_open_moves_starting_to_running() {
        let mut control = Control::default();
        let link = ready(&mut control);
        let (_, actions) = control.local_accept(pid(FACADE));
        assert_eq!(
            actions,
            vec![Action::Facade(link, facade_line(running(false, None)))]
        );
    }

    /// Only the live facade gets a link, and only one link per start.
    /// Each other peer is closed at once, and the facade stays (decision
    /// CH62 rule 10).
    #[test]
    fn refuses_a_peer_that_is_not_the_waiting_facade() {
        let mut control = Control::default();
        // No facade runs before the first hello.
        let (local, actions) = control.local_accept(pid(FACADE));
        assert_eq!(
            actions,
            vec![Action::LocalClose(local, Why::NotFacade(pid(FACADE)))]
        );

        let link = ready(&mut control);
        let (stranger, actions) = control.local_accept(pid(8));
        assert_eq!(
            actions,
            vec![Action::LocalClose(stranger, Why::NotFacade(pid(8)))]
        );
        let first = open(&mut control);
        let (second, actions) = control.local_accept(pid(FACADE));
        assert_eq!(
            actions,
            vec![Action::LocalClose(second, Why::NotFacade(pid(FACADE)))]
        );
        // The first link still relays.
        assert_eq!(
            control.local_line(first, &start())[0],
            Action::Turn(link, turn_line(TurnEvent::Start(TurnStart {})))
        );
    }

    /// A close of the local link kills the facade, and the restart of the
    /// supervisor follows the exit (decision CH32 rule 7). The state stays
    /// `running` until the reap.
    #[test]
    fn a_close_kills_the_facade() {
        let mut control = Control::default();
        let link = ready(&mut control);
        let local = open(&mut control);
        assert_eq!(
            control.local_ended(local),
            vec![
                Action::LocalClose(local, Why::Ended),
                Action::Kill(pid(FACADE))
            ]
        );
        // A second end of the same link changes nothing.
        assert_eq!(control.local_ended(local), vec![]);
        let exited = r#"{"facade":{"exited":{"last_turn_end":null,"starts":1}}}"#;
        assert_eq!(
            control.reaped(pid(FACADE)),
            vec![
                Action::Facade(link, format!("{exited}\n").into_bytes()),
                Action::Kill(pid(FACADE)),
                Action::Timer(RESTART_PAUSE)
            ]
        );
    }

    /// A line that is not one `TurnEvent` closes the local link, so the
    /// facade is killed (decisions CH32 rule 7, CH62 rule 11). So does a
    /// line over the cap.
    #[test]
    fn a_refused_line_closes_the_local_link() {
        // The strict check refuses the first two, and the typed read the
        // rest (decisions CH62 rule 11, CH65 rule 2).
        for (bad, not_strict) in [
            (br#"{"start":{},"start":{}}"#.as_slice(), true),
            (b"", true),
            (br#"{"start":{"now":true}}"#, false),
            (br#"{"end":{"title":"a turn"}}"#, false),
            (br#"{"stop":{}}"#, false),
        ] {
            let mut control = Control::default();
            ready(&mut control);
            let local = open(&mut control);
            let actions = control.local_line(local, bad);
            assert!(
                matches!(
                    actions.as_slice(),
                    [Action::LocalClose(closed, why), Action::Kill(killed)]
                        if *closed == local
                            && *killed == pid(FACADE)
                            && matches!(why, Why::NotStrict) == not_strict
                            && matches!(why, Why::NotStrict | Why::Refused(_))
                ),
                "{actions:?}"
            );
            // A line after the close changes nothing.
            assert_eq!(control.local_line(local, &start()), vec![]);
        }

        let mut control = Control::default();
        ready(&mut control);
        let local = open(&mut control);
        assert_eq!(
            control.local_refused(local, Why::TooLong),
            vec![
                Action::LocalClose(local, Why::TooLong),
                Action::Kill(pid(FACADE))
            ]
        );
    }

    /// A turn start and a turn end each go to the current link as a turn
    /// line, then as the facade state that they change (decisions CH21,
    /// CH43 rule 8).
    #[test]
    fn relays_a_turn_end_to_the_current_control_link() {
        let mut control = Control::default();
        let old = ready(&mut control);
        let local = open(&mut control);
        assert_eq!(
            control.local_line(local, &start()),
            vec![
                Action::Turn(old, turn_line(TurnEvent::Start(TurnStart {}))),
                Action::Facade(old, facade_line(running(true, None))),
            ]
        );

        // A new link replaces the old one, and the end goes to the new one.
        let (new, action) = hello(&mut control);
        assert_eq!(
            action,
            Some(Action::Ready(
                new,
                line(&ControlReady {
                    facade: running(true, None)
                })
            ))
        );
        assert_eq!(
            control.local_line(local, &end_line()),
            vec![
                Action::Turn(new, turn_line(TurnEvent::End(end()))),
                Action::Facade(new, facade_line(running(false, Some(end())))),
            ]
        );
    }

    /// With no control link past its ready, a turn event is dropped, and
    /// nothing goes on a link before its ready (decision CH62 rule 11).
    #[test]
    fn drops_an_event_while_no_control_link_is_up() {
        let mut control = Control::default();
        let link = ready(&mut control);
        let local = open(&mut control);
        control.ended(link);
        assert_eq!(
            control.local_line(local, &start()),
            vec![Action::Dropped(local)]
        );

        // A link between its accept and its ready gets nothing either.
        control.accept();
        assert_eq!(
            control.local_line(local, &end_line()),
            vec![Action::Dropped(local)]
        );
    }

    /// The last turn end stays for each later ready, across a facade
    /// restart too (decisions CH32 rules 4 and 5).
    #[test]
    fn keeps_the_last_turn_end_for_the_next_ready() {
        let mut control = Control::default();
        let link = ready(&mut control);
        let local = open(&mut control);
        control.local_line(local, &start());
        control.local_line(local, &end_line());
        control.ended(link);

        let (next, action) = hello(&mut control);
        assert_eq!(
            action,
            Some(Action::Ready(
                next,
                line(&ControlReady {
                    facade: running(false, Some(end()))
                })
            ))
        );

        // The facade exits, and the next start is `starting` with the end.
        control.local_ended(local);
        control.reaped(pid(FACADE));
        control.timer();
        let actions = control.started(pid(9));
        let starting = r#"{"starting":{"last_turn_end":{"title":"a turn","updated_at":1893456000000},"pid":9,"starts":2}}"#;
        assert_eq!(
            actions,
            vec![Action::Facade(
                next,
                format!(r#"{{"facade":{starting}}}"#)
                    .into_bytes()
                    .into_iter()
                    .chain([b'\n'])
                    .collect()
            )]
        );
    }

    /// The exit of an orphan, for example a tool that daemonized, leaves
    /// the local link of the facade open: only the facade pid closes it
    /// (decision CH19 rule 1).
    #[test]
    fn an_orphan_exit_keeps_the_local_link() {
        let mut control = Control::default();
        let link = ready(&mut control);
        let local = open(&mut control);
        assert_eq!(control.reaped(pid(8)), vec![]);
        assert_eq!(
            control.local_line(local, &start())[0],
            Action::Turn(link, turn_line(TurnEvent::Start(TurnStart {})))
        );
    }

    /// The reap of the facade closes its local link, so a forked child
    /// that holds the socket cannot keep it, and the next facade can open
    /// its own.
    #[test]
    fn the_facade_exit_closes_its_local_link() {
        let mut control = Control::default();
        ready(&mut control);
        let local = open(&mut control);
        let actions = control.reaped(pid(FACADE));
        assert_eq!(
            actions.last(),
            Some(&Action::LocalClose(local, Why::FacadeExited))
        );
        assert_eq!(control.local_line(local, &start()), vec![]);
        control.timer();
        control.started(pid(9));
        let (_, actions) = control.local_accept(pid(9));
        assert!(
            !actions
                .iter()
                .any(|action| matches!(action, Action::LocalClose(..))),
            "{actions:?}"
        );
    }
}
