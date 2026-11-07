//! The fd limit of the vmm (`nofile::raise`) on the limit of the test
//! process. A raise only grows the soft limit, so one test holds every
//! step and no other test sees a smaller limit.

use creche_vmm_confine::nofile::{NofileError, raise};
use rustix::process::{Resource, getrlimit};

#[test]
fn raise_grows_the_soft_limit_to_the_need() {
    let before = getrlimit(Resource::Nofile);
    let soft = before.current.unwrap_or(u64::MAX);
    // A need at or below the soft limit changes nothing.
    raise(soft.min(64)).unwrap();
    assert_eq!(getrlimit(Resource::Nofile).current, before.current);
    if let Some(hard) = before.maximum {
        // A hard limit below the need refuses, and the soft limit stays.
        let short = raise(hard + 1).unwrap_err();
        assert!(matches!(
            short,
            NofileError::Short { hard: got, need } if got == hard && need == hard + 1
        ));
        assert_eq!(getrlimit(Resource::Nofile).current, before.current);
    }
    if before.current.is_some() && before.maximum.is_none_or(|hard| soft < hard) {
        // A need above the soft limit sets the soft limit to the need and
        // keeps the hard limit.
        raise(soft + 1).unwrap();
        let after = getrlimit(Resource::Nofile);
        assert_eq!(after.current, Some(soft + 1));
        assert_eq!(after.maximum, before.maximum);
    }
}
