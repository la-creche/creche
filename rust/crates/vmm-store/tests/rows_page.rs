//! A list page walks the rows of one family once each and ends early under
//! the body cap. Each row of a page carries the VM state that the caller
//! reads. A turn end keeps the first title, and a remove drops the row
//! (decisions CH21, CH21-5, CH32 rule 4, CH33 rule 2, CH34 rule 4, CH79
//! rule 4, gap G30).

use std::convert::Infallible;
use std::fs;

use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::{PAGE_ROWS, Rows};
use creche_wire::{
    CreateSession, HOST_API_BODY_CAP, ListCursor, ListRow, ListSessions, SessionId, SessionPage,
    SessionRow, TurnEnd, VmState,
};

/// A state root in a new temp dir, removed on drop.
struct TempState(StateRoot);

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl TempState {
    fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!("vmm-page-{}-{name}", std::process::id()));
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

/// The session id of row `index`: a fixed UUID with the index at the end.
#[cfg(test)]
fn id(index: usize) -> SessionId {
    format!("0f8e2a4c-1b3d-4e5f-8a9b-{index:012x}")
        .parse()
        .expect("a session id")
}

/// Makes the rows `0..count` in order, each from `cwd_of(index)`. Row
/// `index` belongs to `family_of(index)`.
#[cfg(test)]
fn fill(
    rows: &mut Rows,
    count: usize,
    family_of: impl Fn(usize) -> &'static str,
    cwd_of: impl Fn(usize) -> String,
) {
    for index in 0..count {
        let create: CreateSession = serde_json::from_value(serde_json::json!({
            "created_at": 1000,
            "cwd": cwd_of(index),
            "family": family_of(index),
            "id": id(index).as_str(),
            "origin": {"door": {}},
        }))
        .expect("a create");
        rows.create(create).expect("a row");
    }
}

#[cfg(test)]
fn request(family: &str, cwd: Option<&str>, cursor: Option<ListCursor>) -> ListSessions {
    serde_json::from_value(serde_json::json!({
        "cursor": cursor,
        "cwd": cwd,
        "family": family,
    }))
    .expect("a list request")
}

/// The page of `request`, with every VM `off`.
fn off_page(rows: &Rows, request: &ListSessions) -> SessionPage {
    let Ok(page) = rows.page(request, |_| Ok::<_, Infallible>(VmState::Off));
    page
}

/// Every page of a walk from the first page until `next` is `null`.
#[cfg(test)]
fn walk(rows: &Rows, family: &str, cwd: Option<&str>) -> Vec<SessionPage> {
    let mut pages = Vec::new();
    let mut cursor = None;
    loop {
        let page = off_page(rows, &request(family, cwd, cursor));
        cursor = page.next;
        pages.push(page);
        if cursor.is_none() {
            return pages;
        }
    }
}

#[cfg(test)]
fn ids(pages: &[SessionPage]) -> Vec<SessionId> {
    pages
        .iter()
        .flat_map(|page| page.rows.iter().map(|row| row.id.clone()))
        .collect()
}

// Rows of a second family sit between the rows of the first. A page of
// exactly PAGE_ROWS rows with no row after it has a `null` next.
#[test]
fn a_cursor_walk_returns_each_row_once() {
    let state = TempState::new("walk");
    let mut rows = state.open();
    let family = |index: usize| {
        if index.is_multiple_of(3) {
            "otto"
        } else {
            "anna"
        }
    };
    fill(&mut rows, 3 * PAGE_ROWS, family, |_| "/w".to_owned());

    let pages = walk(&rows, "anna", None);

    let sizes: Vec<usize> = pages.iter().map(|page| page.rows.len()).collect();
    assert_eq!(sizes, [PAGE_ROWS, PAGE_ROWS]);
    let newest_first: Vec<SessionId> = (0..3 * PAGE_ROWS)
        .rev()
        .filter(|index| family(*index) == "anna")
        .map(id)
        .collect();
    assert_eq!(ids(&pages), newest_first);
}

// The filter applies before the cut, so a page holds PAGE_ROWS matching
// rows, not the matches among the newest PAGE_ROWS rows.
#[test]
fn the_cwd_filter_applies_before_the_cut() {
    let state = TempState::new("cwd");
    let mut rows = state.open();
    let cwd = |index: usize| format!("/{}", index % 2);
    fill(&mut rows, 3 * PAGE_ROWS, |_| "anna", cwd);

    let pages = walk(&rows, "anna", Some("/1"));

    let sizes: Vec<usize> = pages.iter().map(|page| page.rows.len()).collect();
    assert_eq!(sizes, [PAGE_ROWS, PAGE_ROWS / 2]);
    assert!(
        pages
            .iter()
            .flat_map(|page| &page.rows)
            .all(|row| row.cwd.as_str() == "/1")
    );
}

// serde_json writes U+0001 in 6 bytes, so a row with a cwd of 4096 such
// characters takes about 24 KiB. 50 of them pass the 1 MiB cap. A cwd of
// plain ASCII takes about 4 KiB, and 50 of those fit.
#[test]
fn rows_with_4096_character_cwds_end_a_page_early() {
    let state = TempState::new("cap");
    let mut rows = state.open();
    let cwd = |index: usize| format!("/{}{index:03}", "\u{1}".repeat(4092));
    fill(&mut rows, PAGE_ROWS, |_| "anna", cwd);

    let pages = walk(&rows, "anna", None);

    let first = pages.first().expect("a page");
    assert!(first.rows.len() < PAGE_ROWS, "{} rows", first.rows.len());
    assert!(first.next.is_some());
    let second = pages.get(1).expect("a second page");
    let mut longer = first.clone();
    longer.rows.extend(second.rows.first().cloned());
    let longer_size = serde_json::to_vec(&longer).expect("a page writes").len();
    assert!(longer_size > HOST_API_BODY_CAP, "{longer_size} bytes");
    for page in &pages {
        let size = serde_json::to_vec(page).expect("a page writes").len();
        assert!(size <= HOST_API_BODY_CAP, "{size} bytes");
    }
    let newest_first: Vec<SessionId> = (0..PAGE_ROWS).rev().map(id).collect();
    assert_eq!(ids(&pages), newest_first);
}

/// The fields of row `index` of the family anna, from `cwd`.
#[cfg(test)]
fn row_json(index: usize, cwd: &str) -> serde_json::Value {
    serde_json::json!({
        "created_at": 1000,
        "cwd": cwd,
        "family": "anna",
        "id": id(index).as_str(),
        "origin": {"door": {}},
        "title": null,
        "updated_at": 1000,
    })
}

/// The bytes of the compact JSON of a page of `rows` with `next` at
/// place 2, each VM `off` as `page` gives it.
#[cfg(test)]
fn page_bytes(rows: Vec<SessionRow>) -> usize {
    let page = SessionPage {
        next: Some(ListCursor::try_from(2).expect("a cursor")),
        rows: rows
            .into_iter()
            .map(|row| ListRow::new(row, VmState::Off))
            .collect(),
    };
    serde_json::to_vec(&page).expect("a page writes").len()
}

// The page ends exactly at the cap. Row 0 is last, row 1 sits at the cut,
// and `filler` rows of about 24 KiB come first. The cwd of row 1 is sized
// so that a page with row 1, and `next` at its place 2, takes
// `HOST_API_BODY_CAP + over` bytes. At `over` 0 row 1 fits. At `over` 1 it
// does not, and `next` points to the last filler at place 3.
#[test]
fn a_page_ends_at_the_exact_body_cap() {
    let filler_cwd = format!("/{}", "\u{1}".repeat(4000));
    for over in [0, 1] {
        let target = HOST_API_BODY_CAP + over;
        let page_of = |fillers: usize, cut_cwd: &str| {
            let mut page: Vec<SessionRow> = (2..2 + fillers)
                .rev()
                .map(|index| serde_json::from_value(row_json(index, &filler_cwd)))
                .collect::<Result<_, _>>()
                .expect("filler rows");
            page.push(serde_json::from_value(row_json(1, cut_cwd)).expect("a cut row"));
            page_bytes(page)
        };
        let fillers = (0..PAGE_ROWS)
            .take_while(|fillers| page_of(*fillers, "/") <= target)
            .last()
            .expect("a filler count");
        // serde_json writes U+0001 in 6 bytes and `a` in 1.
        let extra = target - page_of(fillers, "/");
        let cut_cwd = format!("/{}{}", "\u{1}".repeat(extra / 6), "a".repeat(extra % 6));
        assert_eq!(page_of(fillers, &cut_cwd), target);
        assert!(fillers + 1 < PAGE_ROWS, "{fillers} fillers");

        let state = TempState::new(&format!("exact-{over}"));
        let mut rows = state.open();
        let cwds = [
            vec!["/w".to_owned(), cut_cwd],
            vec![filler_cwd.clone(); fillers],
        ]
        .concat();
        for (index, cwd) in cwds.iter().enumerate() {
            // A create holds the row fields but `title` and `updated_at`.
            let mut json = row_json(index, cwd);
            if let Some(fields) = json.as_object_mut() {
                fields.remove("title");
                fields.remove("updated_at");
            }
            let create: CreateSession = serde_json::from_value(json).expect("a create");
            rows.create(create).expect("a row");
        }

        let page = off_page(&rows, &request("anna", None, None));

        let size = serde_json::to_vec(&page).expect("a page writes").len();
        let fits = over == 0;
        assert_eq!(page.rows.len(), fillers + usize::from(fits));
        assert_eq!(
            page.next.map(ListCursor::get),
            Some(if fits { 2 } else { 3 })
        );
        assert_eq!(size == HOST_API_BODY_CAP, fits, "{size} bytes");
        assert!(size <= HOST_API_BODY_CAP, "{size} bytes");
    }
}

#[test]
fn an_unknown_family_gives_an_empty_page() {
    let state = TempState::new("unknown");
    let mut rows = state.open();
    fill(&mut rows, 3, |_| "anna", |_| "/w".to_owned());

    for cursor in [None, Some(1), Some(4), Some((1 << 53) - 1)] {
        let cursor = cursor.map(|place: u64| ListCursor::try_from(place).expect("a cursor"));
        let page = off_page(&rows, &request("otto", None, cursor));
        assert_eq!((page.next, page.rows.len()), (None, 0));
    }
}

// The reader gets each row of the page in page order, and its state lands
// on that row. The row file keeps no state.
#[test]
fn each_row_carries_the_state_of_the_reader() {
    let state = TempState::new("vm");
    let mut rows = state.open();
    fill(&mut rows, 4, |_| "anna", |_| "/w".to_owned());
    let states = [
        VmState::None,
        VmState::Off,
        VmState::Asleep,
        VmState::Running,
    ];
    let mut asked = Vec::new();

    let page = rows.page(&request("anna", None, None), |row| {
        asked.push(row.id.clone());
        Ok::<_, Infallible>(states[asked.len() - 1])
    });

    let Ok(page) = page;
    let newest_first: Vec<SessionId> = (0..4).rev().map(id).collect();
    assert_eq!(asked, newest_first);
    let vms: Vec<VmState> = page.rows.iter().map(|row| row.vm).collect();
    assert_eq!(vms, states);
    let file = fs::read_to_string(state.0.row_file(&id(0))).expect("a row file");
    assert!(!file.contains("\"vm\""), "{file}");
}

// An error of the reader ends the page with that error, so a page never
// holds a row with no state.
#[test]
fn an_error_of_the_reader_ends_the_page() {
    let state = TempState::new("vm-error");
    let mut rows = state.open();
    fill(&mut rows, 3, |_| "anna", |_| "/w".to_owned());

    let page = rows.page(&request("anna", None, None), |row| {
        if row.id == id(1) {
            Err("no state")
        } else {
            Ok(VmState::Off)
        }
    });

    assert_eq!(page, Err("no state"));
}

#[cfg(test)]
fn turn_end(title: &str, updated_at: u64) -> TurnEnd {
    serde_json::from_value(serde_json::json!({"title": title, "updated_at": updated_at}))
        .expect("a turn end")
}

#[test]
fn a_turn_end_keeps_the_first_title() {
    let state = TempState::new("turn");
    let mut rows = state.open();
    fill(&mut rows, 2, |_| "anna", |_| "/w".to_owned());

    rows.turn_end(&id(0), &turn_end("first", 2000))
        .expect("a write");
    let row = rows
        .turn_end(&id(0), &turn_end("second", 3000))
        .expect("a write")
        .expect("a row");

    assert_eq!(
        row.title.as_ref().map(|title| title.as_str()),
        Some("first")
    );
    assert_eq!(u64::from(row.updated_at), 3000);
    let reloaded = state.open();
    assert_eq!(reloaded.get(&id(0)), Some(&row));
    let order: Vec<SessionId> = reloaded
        .newest_first()
        .map(|(_, row)| row.id.clone())
        .collect();
    assert_eq!(order, [id(1), id(0)]);
    let absent = rows.turn_end(&id(9), &turn_end("x", 1)).expect("no write");
    assert_eq!(absent, None);
}

#[test]
fn a_remove_drops_the_row_and_its_file() {
    let state = TempState::new("remove");
    let mut rows = state.open();
    fill(&mut rows, 2, |_| "anna", |_| "/w".to_owned());

    let removed = rows.remove(&id(0)).expect("a remove").expect("a row");

    assert_eq!(removed.id, id(0));
    assert!(!state.0.row_file(&id(0)).exists());
    assert_eq!(rows.remove(&id(0)).expect("no remove"), None);
    for rows in [rows, state.open()] {
        assert_eq!(rows.get(&id(0)), None);
        assert_eq!(ids(&walk(&rows, "anna", None)), [id(1)]);
    }
}

// A create after a remove takes a new place, never the place of a live row.
// Row 0 comes back as a new row after row 1, and row 2 follows it.
#[test]
fn a_create_after_a_remove_takes_a_new_place() {
    let state = TempState::new("again");
    let mut rows = state.open();
    fill(&mut rows, 2, |_| "anna", |_| "/w".to_owned());
    rows.remove(&id(0)).expect("a remove").expect("a row");

    fill(&mut rows, 3, |_| "anna", |_| "/w".to_owned());

    for rows in [rows, state.open()] {
        let places: Vec<(u64, SessionId)> = rows
            .newest_first()
            .map(|(order, row)| (order.get(), row.id.clone()))
            .collect();
        assert_eq!(places, [(4, id(2)), (3, id(0)), (2, id(1))]);
    }
}
