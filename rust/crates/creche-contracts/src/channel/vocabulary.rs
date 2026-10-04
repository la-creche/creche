//! The closed sets of names that a line of the channel holds (contract 03).
//!
//! Each set is an enum. Components release separately, so a reader can meet a
//! name from a newer writer. The doc comment of each enum says what a reader
//! does with such a name.

use std::fmt;

/// One name of a closed set, and its text on the wire.
pub trait Word: Copy + Sized + 'static {
    /// Each name of the set.
    const ALL: &'static [Self];

    /// The name as the wire holds it.
    fn as_str(self) -> &'static str;

    /// The name that `text` is. `None` for a text outside the set.
    #[must_use]
    fn from_wire(text: &str) -> Option<Self> {
        Self::ALL.iter().copied().find(|word| word.as_str() == text)
    }
}

/// Makes one closed set: the enum, its text on the wire and its `Display`.
macro_rules! words {
    (
        $(#[$attribute:meta])*
        $name:ident {
            $($(#[$variant_attribute:meta])* $variant:ident => $text:literal,)+
        }
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name {
            $($(#[$variant_attribute])* $variant,)+
        }

        impl Word for $name {
            const ALL: &'static [Self] = &[$(Self::$variant,)+];

            fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $text,)+
                }
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(Word::as_str(*self))
            }
        }
    };
}

words! {
    /// The type of a message from the host to the playpen (contract 03 §4).
    ///
    /// A reader refuses a line with an unknown type. The playpen writes one
    /// `log` at level `error` and does not act on the line (§5.3).
    HostType {
        /// Accept the channel.
        Hello => "hello",
        /// Start the pi process of a session before its first prompt.
        OpenSession => "open_session",
        /// Run a turn.
        StartTurn => "start_turn",
        /// Run a turn on a process that the host believes is resident.
        Prompt => "prompt",
        /// Queue a steering message into a turn that runs.
        Steer => "steer",
        /// Abort the turn of one session.
        Abort => "abort",
        /// End the pi process of one session.
        StopProcess => "stop_process",
        /// Read the pi entries of a session back.
        GetEntries => "get_entries",
        /// Liveness probe.
        Ping => "ping",
        /// End each process and exit.
        Shutdown => "shutdown",
    }
}

words! {
    /// The type of a message from the playpen to the host (contract 03 §5).
    ///
    /// A reader refuses a line with an unknown type and counts the refusal
    /// (§13 rules 1 and 2).
    PlaypenType {
        /// The first line of the channel.
        Ready => "ready",
        /// The answer to `open_session`.
        SessionOpened => "session_opened",
        /// One wrapped pi event.
        Event => "event",
        /// The turn ended and pi settled.
        TurnSettled => "turn_settled",
        /// The turn ended and did not settle.
        TurnFailed => "turn_failed",
        /// A pi process ended.
        ProcessExit => "process_exit",
        /// The answer to `ping`.
        Pong => "pong",
        /// Free text.
        Log => "log",
        /// The answer to `get_entries`.
        Entries => "entries",
        /// The playpen cannot serve and exits.
        Fatal => "fatal",
    }
}

words! {
    /// Why the playpen failed a turn (contract 03 §5.3).
    ///
    /// In `turn_failed`, a reader accepts an unknown value and reads it as
    /// [`FailReason::Internal`]: the turn failed, and `internal` is the
    /// conservative outcome. In `entries`, a reader keeps an unknown value as
    /// text.
    FailReason {
        /// `prompt` came with no resident process. The host tries again.
        NoResidentProcess => "no_resident_process",
        /// pi refused `fork`. The host tries again.
        ForkRefused => "fork_refused",
        /// The credential file is older than `env_epoch`.
        StaleCredentials => "stale_credentials",
        /// The pi process did not start.
        PiStartFailed => "pi_start_failed",
        /// pi refused the prompt command.
        PiRejectedPrompt => "pi_rejected_prompt",
        /// The pi process died during the turn.
        ProcessDied => "process_died",
        /// The playpen reached `deadline_s` first.
        DeadlineExceeded => "deadline_exceeded",
        /// A turn already runs on that session.
        SessionBusyInSandbox => "session_busy_in_sandbox",
        /// A line from the host was over the size limit.
        LineTooLarge => "line_too_large",
        /// A defect in the playpen.
        Internal => "internal",
    }
}

impl FailReason {
    /// Whether the host tries the turn again and does not report the reason
    /// (contract 03 §5.3).
    #[must_use]
    pub const fn is_retried(self) -> bool {
        matches!(self, Self::NoResidentProcess | Self::ForkRefused)
    }
}

words! {
    /// Why no pi process is resident after `open_session` (contract 03 §5.6).
    ///
    /// A reader keeps an unknown value as text. The host acts on nothing in a
    /// `session_opened` line.
    OpenReason {
        /// The family holds no process between turns. Not a failure.
        NotHeld => "not_held",
        /// The credential file is older than `env_epoch`.
        StaleCredentials => "stale_credentials",
        /// The pi process did not start.
        PiStartFailed => "pi_start_failed",
        /// A process for that session already starts.
        SessionBusyInSandbox => "session_busy_in_sandbox",
        /// A defect in the playpen, or a workspace link that it cannot make.
        Internal => "internal",
    }
}

words! {
    /// Why a pi process ended (contract 03 §5.4).
    ///
    /// A reader keeps an unknown value as text. A reader reads no value as
    /// [`ExitReason::Crashed`].
    ExitReason {
        /// The playpen reaped an idle process.
        Reaped => "reaped",
        /// The host sent `stop_process`.
        Stopped => "stopped",
        /// The process ended on its own.
        Crashed => "crashed",
        /// The host sent `shutdown`.
        Shutdown => "shutdown",
    }
}

words! {
    /// The level of a `log` line (contract 03 §5.5).
    ///
    /// A reader keeps an unknown value as text. A reader reads no value as
    /// [`LogLevel::Info`].
    LogLevel {
        /// For a developer.
        Debug => "debug",
        /// A fact, also each stderr line of a pi process.
        Info => "info",
        /// The session continues with less than it asked for.
        Warn => "warn",
        /// The playpen did not act on a line of the host.
        Error => "error",
    }
}

words! {
    /// The role of a pi entry that the host matches (contract 03 §5.8 rule
    /// 2).
    ///
    /// pi can use each other name. A reader keeps such a name as text and
    /// ignores the entry.
    Role {
        /// A message of the user.
        User => "user",
        /// A message of the model.
        Assistant => "assistant",
    }
}

words! {
    /// One optional behavior that a playpen supports, in `caps` of `ready`
    /// (contract 03 §3).
    ///
    /// A reader ignores an unknown name. The host acts on
    /// [`Cap::GetEntries`] only (§4.8 rule 9).
    Cap {
        /// The playpen passes `steer` to pi.
        Steer => "steer",
        /// The playpen merges deltas (§9 rule 4).
        Coalesce => "coalesce",
        /// The playpen makes the `code-sandbox` link (§7.2).
        WorkspaceLink => "workspace_link",
        /// The playpen answers `get_entries` (§4.8).
        GetEntries => "get_entries",
    }
}

words! {
    /// Why the playpen cannot serve, as the playpen writes it (contract 03
    /// §5.7).
    ///
    /// The host reads this field as a
    /// [`FatalClaim`](super::claim::FatalClaim), which says what it does with
    /// an unknown value.
    FatalReason {
        /// The control mount is not writable, so the playpen cannot take
        /// its lock.
        ControlMountUnwritable => "control_mount_unwritable",
        /// The environment does not name one of the three directories.
        MountDirUnset => "mount_dir_unset",
    }
}

words! {
    /// The kind of a workspace (contract 03 §7.2).
    ///
    /// A reader refuses an unknown value. The playpen fails the turn with
    /// `internal`.
    WorkspaceKind {
        /// The work directory of one chat session of the `code-sandbox`
        /// family.
        CodeSandbox => "code-sandbox",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn round_trip<T: Word + PartialEq + fmt::Debug + fmt::Display>(names: &[&str]) {
        let written: Vec<&str> = T::ALL.iter().map(|word| word.as_str()).collect();

        assert_eq!(written, names);
        for word in T::ALL {
            assert_eq!(T::from_wire(word.as_str()), Some(*word));
            assert_eq!(word.to_string(), word.as_str());
            assert_eq!(T::from_wire(&word.as_str().to_uppercase()), None);
            assert_eq!(T::from_wire(&format!("{}\n", word.as_str())), None);
        }

        assert_eq!(T::from_wire(""), None);
    }

    #[test]
    fn each_set_has_the_names_of_the_contract() {
        round_trip::<HostType>(&[
            "hello",
            "open_session",
            "start_turn",
            "prompt",
            "steer",
            "abort",
            "stop_process",
            "get_entries",
            "ping",
            "shutdown",
        ]);
        round_trip::<PlaypenType>(&[
            "ready",
            "session_opened",
            "event",
            "turn_settled",
            "turn_failed",
            "process_exit",
            "pong",
            "log",
            "entries",
            "fatal",
        ]);
        round_trip::<FailReason>(&[
            "no_resident_process",
            "fork_refused",
            "stale_credentials",
            "pi_start_failed",
            "pi_rejected_prompt",
            "process_died",
            "deadline_exceeded",
            "session_busy_in_sandbox",
            "line_too_large",
            "internal",
        ]);
        round_trip::<OpenReason>(&[
            "not_held",
            "stale_credentials",
            "pi_start_failed",
            "session_busy_in_sandbox",
            "internal",
        ]);
        round_trip::<ExitReason>(&["reaped", "stopped", "crashed", "shutdown"]);
        round_trip::<LogLevel>(&["debug", "info", "warn", "error"]);
        round_trip::<Role>(&["user", "assistant"]);
        round_trip::<Cap>(&["steer", "coalesce", "workspace_link", "get_entries"]);
        round_trip::<FatalReason>(&["control_mount_unwritable", "mount_dir_unset"]);
        round_trip::<WorkspaceKind>(&["code-sandbox"]);
    }

    #[test]
    fn the_host_tries_two_reasons_again() {
        let retried: Vec<FailReason> = FailReason::ALL
            .iter()
            .copied()
            .filter(|reason| reason.is_retried())
            .collect();

        assert_eq!(
            retried,
            [FailReason::NoResidentProcess, FailReason::ForkRefused]
        );
    }
}
