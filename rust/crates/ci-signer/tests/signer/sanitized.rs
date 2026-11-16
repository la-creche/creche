//! The fixtures name no deployment (root `AGENTS.md`, "Sanitization"). The
//! test reads every file of the fixture directory, `index.tsv` included,
//! and every JSON text inside a JSON string, such as `event_payload`.

use serde_json::Value;

use crate::common::{OWNER, fixture_dir};

/// The logins and user names that a sanitized fixture may hold.
const NAMES: [&str; 3] = [OWNER, "operator", "forgejo-actions"];

/// The documentation range TEST-NET-1 (RFC 5737), the only addresses
/// allowed.
const TEST_NET: &str = "192.0.2.";

/// Each dotted quad of four numbers of 0 to 255 in `text`.
fn addresses(text: &str) -> Vec<String> {
    text.split(|c: char| !(c.is_ascii_digit() || c == '.'))
        .filter(|run| {
            let parts: Vec<&str> = run.split('.').collect();
            parts.len() == 4
                && parts.iter().all(|part| {
                    (1..=3).contains(&part.len()) && part.parse::<u16>().is_ok_and(|n| n <= 255)
                })
        })
        .map(str::to_owned)
        .collect()
}

/// Each UTC offset after a time of day `HH:MM:SS` in `text`.
fn offsets(text: &str) -> Vec<String> {
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    for start in 0..bytes.len().saturating_sub(8) {
        let time = &bytes[start..start + 8];
        let shape = time.iter().enumerate().all(|(i, b)| {
            if i == 2 || i == 5 {
                *b == b':'
            } else {
                b.is_ascii_digit()
            }
        });
        if !shape || (start > 0 && bytes[start - 1] != b'T') {
            continue;
        }
        let mut end = start + 8;
        if bytes.get(end) == Some(&b'.') {
            end += 1;
            while bytes.get(end).is_some_and(u8::is_ascii_digit) {
                end += 1;
            }
        }
        let rest = text.get(end..).unwrap_or_default();
        let offset = if rest.starts_with('Z') {
            "Z"
        } else {
            rest.get(..6).unwrap_or(rest)
        };
        found.push(offset.to_owned());
    }
    found
}

/// Each `login` and `username` value, and each repository owner, in a
/// JSON value and in each JSON text inside one of its strings.
fn names(value: &Value, logins: &mut Vec<String>, owners: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            for (key, item) in map {
                match (key.as_str(), item) {
                    ("login" | "username", Value::String(text)) => logins.push(text.clone()),
                    ("full_name", Value::String(text)) if text.contains('/') => {
                        owners.push(text.split('/').next().unwrap().to_owned());
                    }
                    _ => {}
                }
                names(item, logins, owners);
            }
        }
        Value::Array(items) => items.iter().for_each(|item| names(item, logins, owners)),
        Value::String(text) => {
            if let Ok(inner) = serde_json::from_str::<Value>(text) {
                names(&inner, logins, owners);
            }
        }
        _ => {}
    }
}

#[test]
fn fixtures_are_sanitized() {
    let mut files = 0;
    for entry in std::fs::read_dir(fixture_dir()).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let text = std::fs::read_to_string(&path).unwrap();
        files += 1;

        for address in addresses(&text) {
            assert!(
                address.starts_with(TEST_NET),
                "{name}: the address {address}"
            );
        }
        for offset in offsets(&text) {
            assert!(
                offset == "Z" || offset == "+00:00",
                "{name}: the offset {offset}"
            );
        }
        for part in text.split(['/', '"']).collect::<Vec<_>>().windows(2) {
            if part[0] == "repos" {
                assert_eq!(part[1], OWNER, "{name}: a path names another owner");
            }
        }

        if name.ends_with(".json") {
            let value: Value = serde_json::from_str(&text).unwrap();
            let (mut logins, mut owners) = (Vec::new(), Vec::new());
            names(&value, &mut logins, &mut owners);
            for login in logins {
                assert!(NAMES.contains(&login.as_str()), "{name}: the login {login}");
            }
            for owner in owners {
                assert_eq!(owner, OWNER, "{name}: the owner {owner}");
            }
        }
    }
    assert!(files >= 16, "{files} files");
}

#[test]
fn the_checks_find_what_they_look_for() {
    assert_eq!(
        addresses("http://10.1.2.3:3000/ and 192.0.2.10 and 17.0.1"),
        ["10.1.2.3", "192.0.2.10"]
    );
    assert_eq!(
        offsets("\"2026-11-16T00:33:08-08:00\" \"2026-11-16T08:33:08Z\" 08:33:08"),
        ["-08:00", "Z"]
    );

    let (mut logins, mut owners) = (Vec::new(), Vec::new());
    let value = serde_json::json!({"a": "{\"login\":\"someone\",\"full_name\":\"org/repo\"}"});
    names(&value, &mut logins, &mut owners);
    assert_eq!(
        (logins, owners),
        (vec!["someone".to_owned()], vec!["org".to_owned()])
    );
}
