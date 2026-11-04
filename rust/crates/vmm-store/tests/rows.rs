//! Session rows load after a restart with their stored order, and a create
//! is idempotent (decisions CH1, CH2, CH21-5, CH25 rule 2, gap G22).

use std::fs;
use std::path::PathBuf;

use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::{CreateError, LoadError, Rows};
use creche_wire::{CreateSession, SessionId, SessionRow};

const A: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";
const C: &str = "2b3c4d5e-6f7a-4b2c-9d3e-4f5a6b7c8d9e";

/// A state root in a new temp dir, removed on drop.
struct TempState(StateRoot);

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl TempState {
    fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!("vmm-rows-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        Self(StateRoot::new(path).expect("a state root"))
    }

    fn open(&self) -> Rows {
        Rows::open(self.0.clone()).expect("the rows load")
    }
}

impl Drop for TempState {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(self.0.path());
    }
}

#[cfg(test)]
fn id(text: &str) -> SessionId {
    text.parse().expect("a session id")
}

#[cfg(test)]
fn create(id: &str, created_at: u64, cwd: &str) -> CreateSession {
    serde_json::from_value(create_json(id, created_at, cwd)).expect("a create")
}

#[cfg(test)]
fn create_json(id: &str, created_at: u64, cwd: &str) -> serde_json::Value {
    serde_json::json!({
        "created_at": created_at,
        "cwd": cwd,
        "family": "anna",
        "id": id,
        "origin": {"door": {}},
    })
}

/// The create of `A` at 1000 in `/w`, with `key` set to `value`.
#[cfg(test)]
fn create_with(key: &str, value: serde_json::Value) -> CreateSession {
    let mut json = create_json(A, 1000, "/w");
    if let Some(field) = json.get_mut(key) {
        *field = value;
    }
    serde_json::from_value(json).expect("a create")
}

#[cfg(test)]
fn newest_first(rows: &Rows) -> Vec<(u64, SessionRow)> {
    rows.newest_first()
        .map(|(order, row)| (order.get(), row.clone()))
        .collect()
}

#[test]
fn a_create_makes_a_row_with_no_title() {
    let state = TempState::new("new");
    let mut rows = state.open();

    let row = rows.create(create(A, 1000, "/w")).expect("a row");

    assert_eq!(row.title, None);
    assert_eq!(row.updated_at, row.created_at);
    assert_eq!(rows.get(&id(A)), Some(&row));
    assert!(state.0.row_file(&id(A)).exists());
}

// An abandoned create keeps its row: nothing but a delete removes it
// (decisions CH2, CH25 rule 2).
#[test]
fn a_reload_gives_equal_rows_and_order() {
    let state = TempState::new("reload");
    let mut rows = state.open();
    for (index, session) in [A, B, C].into_iter().enumerate() {
        let created_at = 1000 + u64::try_from(index).expect("a small index");
        rows.create(create(session, created_at, "/w"))
            .expect("a row");
    }
    let before = newest_first(&rows);
    drop(rows);

    let after = newest_first(&state.open());

    assert_eq!(after, before);
    let ids: Vec<&str> = after.iter().map(|(_, row)| row.id.as_str()).collect();
    assert_eq!(ids, [C, B, A]);
}

#[test]
fn two_creates_in_one_millisecond_keep_stored_order() {
    let state = TempState::new("tie");
    let mut rows = state.open();
    rows.create(create(C, 1000, "/w")).expect("a row");
    rows.create(create(A, 1000, "/w")).expect("a row");

    for rows in [rows, state.open()] {
        let ids: Vec<String> = newest_first(&rows)
            .into_iter()
            .map(|(_, row)| row.id.to_string())
            .collect();
        assert_eq!(ids, [A, C]);
    }
}

#[test]
fn a_repeat_create_returns_the_current_row() {
    let state = TempState::new("repeat");
    let mut rows = state.open();
    let first = rows.create(create(A, 1000, "/w")).expect("a row");

    assert_eq!(rows.create(create(A, 1000, "/w")).expect("a row"), first);
    let mut reloaded = state.open();
    assert_eq!(
        reloaded.create(create(A, 1000, "/w")).expect("a row"),
        first
    );
    assert_eq!(newest_first(&reloaded).len(), 1);
}

#[test]
fn a_different_create_is_a_conflict() {
    let state = TempState::new("conflict");
    let mut rows = state.open();
    let first = rows.create(create(A, 1000, "/w")).expect("a row");

    // Each create differs from the first in one field.
    let others = [
        create(A, 1001, "/w"),
        create(A, 1000, "/x"),
        create_with("family", serde_json::json!("otto")),
        create_with(
            "origin",
            serde_json::json!({"delegate": {
                "delegation": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "job": "job-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            }}),
        ),
    ];
    for other in others {
        let error = rows.create(other).expect_err("a conflict");
        assert!(matches!(error, CreateError::Conflict), "{error:?}");
    }
    assert_eq!(state.open().get(&id(A)), Some(&first));
}

#[test]
fn a_leftover_temp_file_is_ignored() {
    let state = TempState::new("temp");
    let mut rows = state.open();
    rows.create(create(A, 1000, "/w")).expect("a row");
    fs::write(state.0.row_temp_file(&id(B)), b"{\"order\":").expect("a temp file");

    let mut rows = state.open();

    assert_eq!(newest_first(&rows).len(), 1);
    rows.create(create(B, 1001, "/w"))
        .expect("a row over the temp file");
    assert_eq!(newest_first(&state.open()).len(), 2);
}

#[test]
fn a_bad_file_refuses_the_load() {
    let cases: [(&str, PathBuf, &[u8]); 2] = [
        (
            "garbage",
            PathBuf::from(format!("{A}.json")),
            b"{\"order\":",
        ),
        ("other", PathBuf::from("notes.txt"), b""),
    ];
    for (name, file, text) in cases {
        let state = TempState::new(name);
        drop(state.open());
        fs::write(state.0.rows_dir().join(file), text).expect("a file");

        let error = Rows::open(state.0.clone()).expect_err("the load fails");

        let parse = matches!(error, LoadError::Parse { .. });
        assert_eq!(parse, name == "garbage", "{error:?}");
    }
}

#[test]
fn a_row_under_another_name_refuses_the_load() {
    let state = TempState::new("misnamed");
    state.open().create(create(A, 1000, "/w")).expect("a row");
    fs::rename(state.0.row_file(&id(A)), state.0.row_file(&id(B))).expect("a rename");

    let error = Rows::open(state.0.clone()).expect_err("the load fails");

    assert!(matches!(error, LoadError::Misnamed(_)), "{error:?}");
}

#[test]
fn two_rows_in_one_place_refuse_the_load() {
    let state = TempState::new("order");
    let mut rows = state.open();
    rows.create(create(A, 1000, "/w")).expect("a row");
    rows.create(create(B, 1001, "/w")).expect("a row");
    let path = state.0.row_file(&id(B));
    let text = fs::read_to_string(&path).expect("the row file");
    fs::write(&path, text.replace("\"order\":2", "\"order\":1")).expect("a write");

    let error = Rows::open(state.0.clone()).expect_err("the load fails");

    assert!(matches!(error, LoadError::OrderTaken(_)), "{error:?}");
}

// A write can fail after its rename, when the dir sync fails, and leave its
// file at its place. So a failed create spends its place, and the next row
// takes the place after it. Two files at one place would refuse the load.
// A dir at the temp path fails the write here, before the rename.
#[test]
fn a_failed_create_spends_its_place() {
    let state = TempState::new("spent");
    let mut rows = state.open();
    fs::create_dir(state.0.row_temp_file(&id(A))).expect("a dir at the temp path");

    let error = rows
        .create(create(A, 1000, "/w"))
        .expect_err("a failed write");
    rows.create(create(B, 1001, "/w")).expect("a row");

    assert!(matches!(error, CreateError::Write(_)), "{error:?}");
    let places: Vec<u64> = newest_first(&rows)
        .iter()
        .map(|(order, _)| *order)
        .collect();
    assert_eq!(places, [2]);
    fs::remove_dir(state.0.row_temp_file(&id(A))).expect("the dir goes");
    rows.create(create(A, 1000, "/w")).expect("a row");
    let reloaded = state.open();
    let places: Vec<u64> = newest_first(&reloaded)
        .iter()
        .map(|(order, _)| *order)
        .collect();
    assert_eq!(places, [3, 2]);
}
