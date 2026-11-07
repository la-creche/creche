//! The fixed paths, ports and waits inside the guest (decisions CH5e, CH46
//! rules 1 and 4, CH62 rules 5, 6, 9, 10 and 13).
//!
//! Guest init and the facade compile these values in. They are plain
//! constants with no serde and no schema entry (decision CH14), so
//! nothing here is generated. `vectors/guest.json` lists the same
//! values. `family_state_cap` there is `crate::FAMILY_STATE_CAP`, the
//! line cap of host port 1100 (decisions CH23 rule 4, CH62 rule 13), and
//! `control_line_cap` is `crate::CONTROL_LINE_CAP`, the line cap of the
//! local link (decision CH62 rule 10). A test holds the file equal to the
//! constants, so the TypeScript agent and the facade can read one
//! definition (decision CH62 rule 5).
//!
//! The session `cwd` is a display value and never one of these paths
//! (decision CH21-4).

use std::time::Duration;

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

/// The guest loopback port of the egress proxy: guest init splices each
/// link to host port 1104 (decision CH46 rule 1). The coding flavor names
/// it in `CRECHE_TOOL_HTTPS_PROXY`, and a creche test holds the two equal.
/// A renumber bumps `GUEST_INIT_PROTOCOL`.
pub const EGRESS_PROXY_PORT: u16 = 3128;

/// The guest loopback port of the git mirror, the `git://` default:
/// guest init splices each link to host port 1103 (decision CH46 rule 4).
pub const MIRROR_PORT: u16 = 9418;

/// How long the facade has to exit after the stop line on its local
/// link, before guest init sends SIGTERM (decision CH62 rule 9, which
/// amends CH19 rule 3 from 30 s to 25 s). The facade bounds its own stop
/// under it.
pub const FACADE_STOP_WAIT: Duration = Duration::from_secs(25);

/// The env name that carries the session id to the facade. It also marks
/// the facade as running under guest init (decision CH62 rule 6).
pub const SESSION_ID_ENV: &str = "CRECHE_SESSION_ID";

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    use serde_json::{Value, json};

    use crate::{CONTROL_LINE_CAP, FAMILY_STATE_CAP};

    use super::{
        CHAPERONE_PORT, EGRESS_PROXY_PORT, FACADE_PROGRAM, FACADE_STOP_WAIT, FAMILY_STATE_SOCKET,
        FLUE_DB, LITELLM_PORT, LOCAL_LINK_SOCKET, MIRROR_PORT, RELAY_SOCKET, SESSION_DISK_MOUNT,
        SESSION_ID_ENV, WORKSPACE,
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
            ("family_state_cap", json!(FAMILY_STATE_CAP)),
            ("control_line_cap", json!(CONTROL_LINE_CAP)),
            ("facade_stop_wait_ms", json!(FACADE_STOP_WAIT.as_millis())),
            ("relay_socket", json!(RELAY_SOCKET)),
            ("litellm_port", json!(LITELLM_PORT)),
            ("chaperone_port", json!(CHAPERONE_PORT)),
            ("egress_proxy_port", json!(EGRESS_PROXY_PORT)),
            ("mirror_port", json!(MIRROR_PORT)),
            ("session_id_env", json!(SESSION_ID_ENV)),
        ]
        .into_iter()
        .map(|(name, value)| (name.to_owned(), value))
        .collect();

        assert_eq!(file, constants);
    }

    /// The file holds the cap of host port 1100 in bytes: 256 KiB
    /// (decision CH23 rule 4). The facade caps each reply with it.
    #[test]
    fn the_vector_file_names_the_state_cap() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(VECTOR_FILE);
        let text = std::fs::read_to_string(path).unwrap();
        let file: BTreeMap<String, Value> = serde_json::from_str(&text).unwrap();

        assert_eq!(file["family_state_cap"], json!(262_144));
        assert_eq!(FAMILY_STATE_CAP, 262_144);
    }

    #[test]
    fn the_session_files_lie_under_the_mount() {
        assert_eq!(FLUE_DB, format!("{SESSION_DISK_MOUNT}/flue.db"));
        assert_eq!(WORKSPACE, format!("{SESSION_DISK_MOUNT}/workspace"));
    }
}
