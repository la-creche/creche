//! The closed vocabularies of contract 05: each one is an enum.
//!
//! Components release separately, so a reader can get a word from a newer
//! writer. The valid type [`super::document::StatusDocument`] refuses a word
//! that its enum does not hold. A view of [`super::views`] does what its
//! Python reader does with such a word, and the doc comment of the view says
//! what that is.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

/// Makes one closed vocabulary: the enum, its words and its error type.
macro_rules! words {
    (
        $(#[$attribute:meta])*
        $name:ident, $error:ident, $noun:literal {
            $($(#[$variant_attribute:meta])* $variant:ident => $word:literal,)+
        }
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name {
            $($(#[$variant_attribute])* $variant,)+
        }

        impl $name {
            /// Each value, in the order of the contract.
            pub const ALL: &[Self] = &[$(Self::$variant,)+];

            /// The word of the value in a file.
            #[must_use]
            pub const fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $word,)+
                }
            }
        }

        impl FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                match text {
                    $($word => Ok(Self::$variant),)+
                    _ => Err($error),
                }
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        #[doc = concat!("A text is not ", $noun, ".")]
        ///
        /// The error does not hold the text. The text is untrusted, and a
        /// caller writes this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $error;

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(concat!("the text is not ", $noun))
            }
        }

        impl Error for $error {}
    };
}

words! {
    /// The kind of a family (contract 05 §2.1).
    ///
    /// A reader that gets another word refuses the document. The Python
    /// reader of `attendance` reads another word as `attended`.
    Kind, KindError, "a family kind" {
        /// A person talks to the family in sessions.
        Attended => "attended",
        /// The family runs one short job for each call.
        Thin => "thin",
        /// A trigger starts the family.
        Autonomous => "autonomous",
    }
}

words! {
    /// The state of a family (contract 05 §3).
    ///
    /// A reader that gets another word refuses the document. The Python
    /// reader of `attendance` reads another word as `in_sync`.
    FamilyState, FamilyStateError, "a family state" {
        /// `caregiver` applied the newest registry revision. No fault is open.
        InSync => "in_sync",
        /// A reconcile pass is at work on the family.
        Reconciling => "reconciling",
        /// The family file of the newest revision did not pass the validator.
        Invalid => "invalid",
        /// One fault or more is open for the family.
        Degraded => "degraded",
    }
}

words! {
    /// The lifecycle state of a sandbox (contract 05 §4.2).
    ///
    /// The valid type refuses a document with another word. The view of
    /// `attendance` drops a row with another word and keeps the document, so
    /// `attendance` does not dial that sandbox.
    SandboxLifecycle, SandboxLifecycleError, "a sandbox state" {
        /// The ledger of `caregiver` holds the row. No VM exists for it.
        Planned => "planned",
        /// The VM exists. `attendance` has no handshake with it yet.
        Creating => "creating",
        /// `attendance` has a handshake with the sandbox. New turns go to it.
        Ready => "ready",
        /// A newer sandbox takes each new turn. The turns of this sandbox go
        /// on to their end.
        Draining => "draining",
        /// The sandbox has no turn. Its removal is in progress.
        Stopping => "stopping",
        /// The VM does not exist now.
        Gone => "gone",
        /// The sandbox did not become `ready`. `caregiver` does not try it
        /// again.
        Failed => "failed",
    }
}

words! {
    /// Whether the VM of a sandbox runs (contract 05 §4.1).
    ///
    /// A reader that gets another word refuses the document.
    SandboxPower, SandboxPowerError, "a sandbox power state" {
        /// The VM runs.
        Running => "running",
        /// The VM does not run.
        Stopped => "stopped",
    }
}

words! {
    /// The state of the channel to a sandbox (contract 05 §4.1).
    ///
    /// A reader that gets another word refuses the document.
    ChannelState, ChannelStateError, "a channel state" {
        /// `attendance` holds a channel to the sandbox.
        Open => "open",
        /// No channel is open.
        Closed => "closed",
        /// The channel dropped, and no new channel is open.
        Lost => "lost",
    }
}

words! {
    /// The state of a credential rotation (contract 05 §6.1).
    ///
    /// A reader that gets another word refuses the document.
    RotationState, RotationStateError, "a rotation state" {
        /// No rotation runs.
        Settled => "settled",
        /// The old credentials are still valid.
        Rotating => "rotating",
        /// The last rotation failed.
        Failed => "failed",
    }
}

words! {
    /// The service that detected a fault (contract 05 §3.3).
    ///
    /// The valid type refuses a document with another word. Each view of the
    /// status document keeps the text.
    FaultSource, FaultSourceError, "a fault source" {
        /// `caregiver`.
        Managerd => "managerd",
        /// `attendance`.
        Sessiond => "sessiond",
        /// The chaperone.
        Pep => "pep",
    }
}

words! {
    /// The code of a fault (contract 05 §3.3).
    ///
    /// The valid type refuses a document with another word. Each view of the
    /// status document keeps the text. The reader of a fault file drops an
    /// entry with another word and keeps the file (§3.3.1 rule 6).
    FaultCode, FaultCodeError, "a fault code" {
        /// The playpen and `attendance` do not share a major version of the
        /// channel protocol.
        ProtocolMismatch => "protocol_mismatch",
        /// Each try to start a sandbox failed.
        SandboxStartFailed => "sandbox_start_failed",
        /// The model proxy answered a call for the family key with an error.
        KeyMintFailed => "key_mint_failed",
        /// The family token did not get to the chaperone.
        TokenPublishFailed => "token_publish_failed",
        /// The chaperone works with an old grant file.
        GrantsStale => "grants_stale",
        /// The playpen sent more messages that `attendance` refused than
        /// `attendance` permits.
        ProtocolViolation => "protocol_violation",
        /// `caregiver` could not set or prove the egress policy of a sandbox.
        EgressAssertFailed => "egress_assert_failed",
        /// The host has no image for the flavor that the family file names.
        ImageFlavorUnconfigured => "image_flavor_unconfigured",
        /// The watch of `caregiver` got no answer from the chaperone.
        PepUnreachable => "pep_unreachable",
        /// `caregiver` could not read the spend of the family.
        SpendUnknown => "spend_unknown",
        /// systemd does not report a timer of the family as enabled.
        TimerEnableFailed => "timer_enable_failed",
        /// Processes of an old channel still run in the sandbox.
        OrphanProcesses => "orphan_processes",
        /// The sandbox runs an older image than the host has.
        ImageBehind => "image_behind",
        /// `attendance` gets no record from the audit file.
        AuditUnreadable => "audit_unreadable",
        /// An MCP server of the family is not on the host, and `caregiver`
        /// sends no new request for it.
        McpInstallHeld => "mcp_install_held",
    }
}

impl FaultCode {
    /// Whether the fault stops new turns: the table of contract 05 §3.3.
    #[must_use]
    pub const fn blocks_turns(self) -> bool {
        match self {
            Self::ProtocolMismatch
            | Self::SandboxStartFailed
            | Self::KeyMintFailed
            | Self::TokenPublishFailed
            | Self::GrantsStale
            | Self::ProtocolViolation
            | Self::EgressAssertFailed => true,
            Self::ImageFlavorUnconfigured
            | Self::PepUnreachable
            | Self::SpendUnknown
            | Self::TimerEnableFailed
            | Self::OrphanProcesses
            | Self::ImageBehind
            | Self::AuditUnreadable
            | Self::McpInstallHeld => false,
        }
    }

    /// Whether `source` is a service that detects this fault (contract 05
    /// §3.3.1). Two services detect `sandbox_start_failed`. Each other code
    /// has one service.
    #[must_use]
    pub const fn is_detected_by(self, source: FaultSource) -> bool {
        match self {
            Self::ProtocolMismatch
            | Self::ProtocolViolation
            | Self::OrphanProcesses
            | Self::AuditUnreadable => matches!(source, FaultSource::Sessiond),
            Self::GrantsStale => matches!(source, FaultSource::Pep),
            Self::SandboxStartFailed => {
                matches!(source, FaultSource::Managerd | FaultSource::Sessiond)
            }
            Self::KeyMintFailed
            | Self::TokenPublishFailed
            | Self::EgressAssertFailed
            | Self::ImageFlavorUnconfigured
            | Self::PepUnreachable
            | Self::SpendUnknown
            | Self::TimerEnableFailed
            | Self::ImageBehind
            | Self::McpInstallHeld => matches!(source, FaultSource::Managerd),
        }
    }
}

words! {
    /// The step of a reconcile pass that is in flight (contract 05 §3.4).
    ///
    /// The set holds the eight words of §3.4 and one word that only
    /// `caregiver` has: `write_timers`. A reader that gets another word
    /// refuses the document.
    ReconcileStep, ReconcileStepError, "a reconcile step" {
        /// The validator checks the family file.
        Validate => "validate",
        /// `caregiver` writes the grant file.
        WriteGrants => "write_grants",
        /// `caregiver` updates the family key.
        UpdateKey => "update_key",
        /// `caregiver` writes the family config mount.
        WriteConfig => "write_config",
        /// `caregiver` changes the egress of the running sandbox.
        SetEgress => "set_egress",
        /// `caregiver` creates a sandbox.
        CreateSandbox => "create_sandbox",
        /// `caregiver` asks `attendance` to switch sandboxes.
        SwitchSandbox => "switch_sandbox",
        /// `caregiver` destroys the old sandbox.
        DestroySandbox => "destroy_sandbox",
        // CONTRACT-QUESTION: contract 05 §3.4 names the eight steps above.
        // `caregiver.reconcile` also publishes `write_timers` as the step of
        // a pass that changed the timer set last. The type takes that word,
        // so a writer with this type can publish each document that
        // `caregiver` writes today. To refuse it, `caregiver` must publish
        // another step for such a pass.
        /// `caregiver` writes the timer set of the triggers.
        WriteTimers => "write_timers",
    }
}

words! {
    /// What the watch of the chaperone last saw (contract 05 §2.2).
    ///
    /// A reader that gets another word refuses the document.
    WatchState, WatchStateError, "a watch state" {
        /// No watch runs.
        Off => "off",
        /// The health check answered.
        Ok => "ok",
        /// The chaperone gave no answer to the last health checks.
        Unreachable => "unreachable",
    }
}

words! {
    /// The window of the family budget (contract 05 §7).
    ///
    /// A reader that gets another word refuses the document.
    SpendWindow, SpendWindowError, "a spend window" {
        /// The budget of one day.
        Day => "day",
    }
}

words! {
    /// The authority that a spend number comes from (contract 05 §7 rule 2).
    ///
    /// A reader that gets another word refuses the document.
    SpendSource, SpendSourceError, "a spend source" {
        /// The model proxy.
        Litellm => "litellm",
    }
}

words! {
    /// What the noticeboard shows for a family: the four states of contract
    /// 05 §3 and the two states of a reader.
    ///
    /// The noticeboard shows a state word that is not one of the four as
    /// `unreadable`. No file holds `unknown` or `unreadable`.
    Health, HealthError, "a family health" {
        /// The state `in_sync`.
        InSync => "in_sync",
        /// The state `reconciling`.
        Reconciling => "reconciling",
        /// The state `invalid`.
        Invalid => "invalid",
        /// The state `degraded`.
        Degraded => "degraded",
        /// The document is stale, or it has no time (§2 rule 5).
        Unknown => "unknown",
        /// The noticeboard cannot read the document, or its state.
        Unreadable => "unreadable",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_word_reads_back_as_its_value() {
        fn round_trip<W>(all: &[W], as_str: fn(W) -> &'static str)
        where
            W: FromStr + Copy + PartialEq + fmt::Debug,
            W::Err: fmt::Debug,
        {
            for value in all {
                assert_eq!(as_str(*value).parse::<W>().unwrap(), *value);
            }
        }

        round_trip(Kind::ALL, Kind::as_str);
        round_trip(FamilyState::ALL, FamilyState::as_str);
        round_trip(SandboxLifecycle::ALL, SandboxLifecycle::as_str);
        round_trip(SandboxPower::ALL, SandboxPower::as_str);
        round_trip(ChannelState::ALL, ChannelState::as_str);
        round_trip(RotationState::ALL, RotationState::as_str);
        round_trip(FaultSource::ALL, FaultSource::as_str);
        round_trip(FaultCode::ALL, FaultCode::as_str);
        round_trip(ReconcileStep::ALL, ReconcileStep::as_str);
        round_trip(WatchState::ALL, WatchState::as_str);
        round_trip(SpendWindow::ALL, SpendWindow::as_str);
        round_trip(SpendSource::ALL, SpendSource::as_str);
        round_trip(Health::ALL, Health::as_str);
    }

    #[test]
    fn a_word_outside_the_vocabulary_is_refused() {
        for text in [
            "",
            "Attended",
            "attended\n",
            " attended",
            "robot",
            "in sync",
        ] {
            assert_eq!(text.parse::<Kind>(), Err(KindError), "{text:?}");
            assert_eq!(
                text.parse::<FamilyState>(),
                Err(FamilyStateError),
                "{text:?}"
            );
        }

        assert_eq!(
            "READY".parse::<SandboxLifecycle>(),
            Err(SandboxLifecycleError)
        );
        assert_eq!("sandbox_lost".parse::<FaultCode>(), Err(FaultCodeError));
        assert_eq!("root".parse::<FaultSource>(), Err(FaultSourceError));
        assert_eq!("wait".parse::<ReconcileStep>(), Err(ReconcileStepError));
        assert_eq!("unknown".parse::<FamilyState>(), Err(FamilyStateError));
    }

    #[test]
    fn each_vocabulary_has_the_count_of_words_of_the_contract() {
        assert_eq!(Kind::ALL.len(), 3);
        assert_eq!(FamilyState::ALL.len(), 4);
        assert_eq!(SandboxLifecycle::ALL.len(), 7);
        assert_eq!(FaultCode::ALL.len(), 15);
        assert_eq!(FaultSource::ALL.len(), 3);
        // The eight steps of §3.4, and `write_timers` of `caregiver`.
        assert_eq!(ReconcileStep::ALL.len(), 9);
        assert_eq!(Health::ALL.len(), 6);
    }

    #[test]
    fn seven_codes_stop_turns() {
        let blocking: Vec<&str> = FaultCode::ALL
            .iter()
            .filter(|code| code.blocks_turns())
            .map(|code| code.as_str())
            .collect();

        assert_eq!(
            blocking,
            [
                "protocol_mismatch",
                "sandbox_start_failed",
                "key_mint_failed",
                "token_publish_failed",
                "grants_stale",
                "protocol_violation",
                "egress_assert_failed",
            ]
        );
    }

    #[test]
    fn each_code_has_one_source_but_sandbox_start_failed() {
        let codes_of = |source: FaultSource| -> Vec<&str> {
            FaultCode::ALL
                .iter()
                .filter(|code| code.is_detected_by(source))
                .map(|code| code.as_str())
                .collect()
        };

        assert_eq!(
            codes_of(FaultSource::Sessiond),
            [
                "protocol_mismatch",
                "sandbox_start_failed",
                "protocol_violation",
                "orphan_processes",
                "audit_unreadable",
            ]
        );
        assert_eq!(codes_of(FaultSource::Pep), ["grants_stale"]);
        assert_eq!(codes_of(FaultSource::Managerd).len(), 10);
        for code in FaultCode::ALL {
            let sources = FaultSource::ALL
                .iter()
                .filter(|source| code.is_detected_by(**source))
                .count();
            let expected = if *code == FaultCode::SandboxStartFailed {
                2
            } else {
                1
            };

            assert_eq!(sources, expected, "{code}");
        }
    }

    #[test]
    fn the_error_is_a_std_error() {
        let boxed: Box<dyn Error> = Box::new(KindError);

        assert_eq!(boxed.to_string(), "the text is not a family kind");
        assert_eq!(Kind::Thin.to_string(), "thin");
    }
}
