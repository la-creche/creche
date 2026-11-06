//! The fixed paths and ports inside the guest (decisions CH5e, CH62
//! rules 5, 6, 10 and 13).
//!
//! Guest init and the facade compile these values in. They are plain
//! constants with no serde and no schema entry (decision CH14), so
//! nothing here is generated. `vectors/guest.json` lists the same
//! values. A test holds the two equal, so the TypeScript agent can read
//! one definition (decision CH62 rule 5).
//!
//! The session `cwd` is a display value and never one of these paths
//! (decision CH21-4).

/// Where guest init mounts the session disk (decision CH62 rule 5).
pub const SESSION_DISK_MOUNT: &str = "/var/lib/creche/session";

/// The facade database, on the session disk (decision CH62 rule 5).
pub const FLUE_DB: &str = "/var/lib/creche/session/flue.db";

/// The workspace on the session disk. It is the cwd of the facade
/// (decision CH62 rule 5).
pub const WORKSPACE: &str = "/var/lib/creche/session/workspace";

/// The facade program in the root disk (decision CH62 rule 5, question
/// Q54-5).
pub const FACADE_PROGRAM: &str = "/usr/local/bin/creche-facade";

/// The local link: the Unix socket that guest init listens on and the
/// live facade connects to (decision CH62 rule 10).
pub const LOCAL_LINK_SOCKET: &str = "/run/creche/guest-init.sock";

/// The family-state socket: guest init splices each connection to a new
/// vsock dial of host port 1100 (decision CH62 rule 13).
pub const FAMILY_STATE_SOCKET: &str = "/run/creche/family-state.sock";

/// The relay socket of the facade, where guest init joins each ACP link
/// of guest port 1201 (decision CH62 rule 5, question Q54-5). The path
/// is the one the facade uses today, under `TMPDIR=/tmp`.
pub const RELAY_SOCKET: &str = "/tmp/creche-facade/acp.sock";

/// The guest loopback port of the LiteLLM plane (decision CH6-2).
pub const LITELLM_PORT: u16 = 4000;

/// The guest loopback port of the chaperone plane (decision CH6-2).
pub const CHAPERONE_PORT: u16 = 8300;

/// The env name that carries the session id to the facade. It also marks
/// the facade as running under guest init (decision CH62 rule 6).
pub const SESSION_ID_ENV: &str = "CRECHE_SESSION_ID";

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    use serde_json::{Value, json};

    use super::{
        CHAPERONE_PORT, FACADE_PROGRAM, FAMILY_STATE_SOCKET, FLUE_DB, LITELLM_PORT,
        LOCAL_LINK_SOCKET, RELAY_SOCKET, SESSION_DISK_MOUNT, SESSION_ID_ENV, WORKSPACE,
    };

    /// The file, relative to the crate.
    const VECTOR_FILE: &str = "vectors/guest.json";

    #[test]
    fn the_vector_file_matches_the_constants() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(VECTOR_FILE);
        let text = std::fs::read_to_string(path).unwrap();
        let file: BTreeMap<String, Value> = serde_json::from_str(&text).unwrap();

        let constants: BTreeMap<String, Value> = [
            ("session_disk_mount", json!(SESSION_DISK_MOUNT)),
            ("flue_db", json!(FLUE_DB)),
            ("workspace", json!(WORKSPACE)),
            ("facade_program", json!(FACADE_PROGRAM)),
            ("local_link_socket", json!(LOCAL_LINK_SOCKET)),
            ("family_state_socket", json!(FAMILY_STATE_SOCKET)),
            ("relay_socket", json!(RELAY_SOCKET)),
            ("litellm_port", json!(LITELLM_PORT)),
            ("chaperone_port", json!(CHAPERONE_PORT)),
            ("session_id_env", json!(SESSION_ID_ENV)),
        ]
        .into_iter()
        .map(|(name, value)| (name.to_owned(), value))
        .collect();

        assert_eq!(file, constants);
    }

    #[test]
    fn the_session_files_lie_under_the_mount() {
        assert_eq!(FLUE_DB, format!("{SESSION_DISK_MOUNT}/flue.db"));
        assert_eq!(WORKSPACE, format!("{SESSION_DISK_MOUNT}/workspace"));
    }
}
