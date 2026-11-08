//! The ELF rule of `static_elf` on made headers, and the JSON shape of
//! `Report` (decision CH76 rule 2). `tests/verify.rs` runs the mode, and
//! `tests/kvm_verify.rs` runs it on the CI build of guest init.

use std::fs::{self, File};
use std::path::PathBuf;

use super::{Check, ElfFault, Report, static_elf};

/// A scratch file that its drop removes.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

/// `static_elf` on a file of `bytes`.
fn check(name: &str, bytes: &[u8]) -> Result<(), ElfFault> {
    let (_scratch, file) = open(name, bytes);
    static_elf(&file)
}

/// A scratch file of `bytes`, open for a read.
fn open(name: &str, bytes: &[u8]) -> (Scratch, File) {
    let path = std::env::temp_dir().join(format!("creche-vmm-elf-{}-{name}", std::process::id()));
    let scratch = Scratch(path);
    fs::write(&scratch.0, bytes).unwrap();
    let file = File::open(&scratch.0).unwrap();
    (scratch, file)
}

/// A 64-bit little-endian x86_64 header of type `kind` with one program
/// header of type `p_type` right after it.
fn elf(kind: u16, p_type: u32) -> Vec<u8> {
    let mut bytes = vec![0_u8; 64 + 56];
    bytes[..6].copy_from_slice(&[0x7f, b'E', b'L', b'F', 2, 1]);
    bytes[0x10..0x12].copy_from_slice(&kind.to_le_bytes());
    bytes[0x12..0x14].copy_from_slice(&62_u16.to_le_bytes());
    bytes[0x20..0x28].copy_from_slice(&64_u64.to_le_bytes());
    bytes[0x36..0x38].copy_from_slice(&56_u16.to_le_bytes());
    bytes[0x38..0x3a].copy_from_slice(&1_u16.to_le_bytes());
    bytes[64..68].copy_from_slice(&p_type.to_le_bytes());
    bytes
}

/// `ET_EXEC` and the `ET_DYN` of a static-pie musl build pass with a
/// `PT_LOAD` header and no `PT_INTERP`.
#[test]
fn a_static_program_passes() {
    assert_eq!(check("exec", &elf(2, 1)), Ok(()));
    assert_eq!(check("pie", &elf(3, 1)), Ok(()));
}

#[test]
fn a_loader_header_fails() {
    assert_eq!(check("interp", &elf(3, 3)), Err(ElfFault::Dynamic));
}

#[test]
fn a_file_that_is_no_x86_64_program_fails() {
    assert_eq!(check("empty", b""), Err(ElfFault::NotElf));
    assert_eq!(check("script", b"#!/bin/sh\n"), Err(ElfFault::NotElf));
    let mut class32 = elf(2, 1);
    class32[4] = 1;
    assert_eq!(check("class32", &class32), Err(ElfFault::NotElf64));
    assert_eq!(check("object", &elf(1, 1)), Err(ElfFault::NotProgram(1)));
    let mut arm = elf(2, 1);
    arm[0x12..0x14].copy_from_slice(&183_u16.to_le_bytes());
    assert_eq!(check("arm", &arm), Err(ElfFault::NotX86_64(183)));
}

#[test]
fn a_header_table_past_the_end_fails() {
    let mut cut = elf(2, 1);
    cut.truncate(64 + 2);
    assert_eq!(check("cut", &cut), Err(ElfFault::BadHeaders));
    let mut size = elf(2, 1);
    size[0x36..0x38].copy_from_slice(&32_u16.to_le_bytes());
    assert_eq!(check("size", &size), Err(ElfFault::BadHeaders));
    let mut far = elf(2, 1);
    far[0x20..0x28].copy_from_slice(&(1_u64 << 40).to_le_bytes());
    assert_eq!(check("far", &far), Err(ElfFault::BadHeaders));
}

/// The report is one line: `ok` is true only when every check passed.
#[test]
fn the_report_is_one_json_line() {
    let report = Report::new(vec![
        Check::pass("a", "fine"),
        Check::fail("b", "line\nbreak"),
    ]);
    assert!(!report.passed());
    let line = report.line().unwrap();
    assert!(!line.contains('\n'), "{line}");
    let value: serde_json::Value = serde_json::from_str(&line).unwrap();
    assert_eq!(
        value,
        serde_json::json!({"ok": false, "checks": [
            {"name": "a", "ok": true, "detail": "fine"},
            {"name": "b", "ok": false, "detail": "line\nbreak"},
        ]})
    );
    assert!(Report::new(vec![Check::pass("a", "fine")]).passed());
}
