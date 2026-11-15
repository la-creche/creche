//! The snapshot read of the kvm_ proofs (openshell ideas plan, idea 1,
//! section 3.6): how many strings in the files of one snapshot generation
//! hold a value, as one-byte text and as UTF-16LE text. `strings` reads
//! each file and `grep -c -F` counts, so a memory file of 1 GiB takes
//! seconds in a debug build. The value goes to `grep` in a pattern file,
//! not on argv. Only `tests/kvm_guest.rs` and `tests/kvm_facade.rs`
//! declare this module, and each uses each item.
#![cfg(test)]

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};

/// The host `strings` and `grep` of the runner.
const STRINGS: &str = "/usr/bin/strings";
const GREP: &str = "/usr/bin/grep";

/// The encodings of `strings`: `s` for one byte per character, `l` for
/// UTF-16LE, the two forms that S0 found in a real snapshot.
const ENCODINGS: [&str; 2] = ["s", "l"];

/// The strings in the files of `generation` that hold `value`, in both
/// encodings. `work` holds the pattern file.
pub fn count(generation: &Path, work: &Path, value: &str) -> usize {
    let pattern = work.join("snapshot-pattern");
    fs::write(&pattern, format!("{value}\n")).unwrap();
    let mut found = 0;
    for entry in fs::read_dir(generation).unwrap() {
        let file = entry.unwrap().path();
        if !file.is_file() {
            continue;
        }
        for encoding in ENCODINGS {
            let mut strings = Command::new(STRINGS)
                .args(["-a", "-e", encoding])
                .arg(&file)
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            let grep = Command::new(GREP)
                .args(["-c", "-F", "-f"])
                .arg(&pattern)
                .stdin(strings.stdout.take().unwrap())
                .output()
                .unwrap();
            assert!(strings.wait().unwrap().success(), "strings {file:?}");
            // grep exits 1 when no line matches, and 2 on an error.
            assert!(grep.status.code().is_some_and(|code| code < 2), "{grep:?}");
            let text = String::from_utf8(grep.stdout).unwrap();
            found += text.trim().parse::<usize>().unwrap();
        }
    }
    fs::remove_file(&pattern).unwrap();
    found
}
