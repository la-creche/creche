//! Whether the host knows a time zone, as `zoneinfo.ZoneInfo(key)` of Python
//! answers on a host with no `tzdata` package.
//!
//! The Python validator takes a zone that the time zone database of the host
//! holds (contract 01 §3.15). The answer depends on the file system of the
//! host, so no type can hold it.

use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

/// The directories that Python searches, in order.
const TZPATH: [&str; 4] = [
    "/usr/share/zoneinfo",
    "/usr/lib/zoneinfo",
    "/usr/share/lib/zoneinfo",
    "/etc/zoneinfo",
];

/// The first bytes of a time zone file.
const MAGIC: &[u8] = b"TZif";

/// The count of bytes in the header of a time zone file.
const HEADER: usize = 44;

/// Which time zones a host knows.
pub trait ZoneFacts {
    /// Whether the time zone database holds `key`.
    fn knows(&self, key: &str) -> bool;
}

/// The time zone database of a host: the directories that hold it, in the
/// order of the search.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SystemZones {
    roots: Vec<PathBuf>,
}

impl SystemZones {
    /// The database of this host: the directories of [`TZPATH`].
    #[must_use]
    pub fn host() -> Self {
        Self {
            roots: TZPATH.iter().map(PathBuf::from).collect(),
        }
    }
}

/// Whether `key` is a relative path in its shortest form with no `..`, as
/// Python demands of a key (`zoneinfo._tzpath._validate_tzfile_path`).
fn is_plain_key(key: &str) -> bool {
    if key.is_empty() || key.contains('\0') || key.ends_with('/') || key.contains("//") {
        return false;
    }

    Path::new(key)
        .components()
        .all(|part| matches!(part, Component::Normal(_)))
        && !key.split('/').any(|part| part == ".")
}

/// Whether `path` is a file that starts as a time zone file starts.
fn is_zone_file(path: &Path) -> Option<bool> {
    if !path.is_file() {
        return None;
    }

    let mut header = [0_u8; HEADER];
    let read = File::open(path)
        .and_then(|mut file| file.read_exact(&mut header))
        .is_ok();

    Some(read && header.starts_with(MAGIC))
}

impl ZoneFacts for SystemZones {
    fn knows(&self, key: &str) -> bool {
        if !is_plain_key(key) {
            return false;
        }

        // Python reads the first file that it finds, and searches no more.
        self.roots
            .iter()
            .find_map(|root| is_zone_file(&root.join(key)))
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::{SystemZones, ZoneFacts, is_plain_key};
    use crate::scratch::Scratch;

    /// A time zone file that Python reads: one zone with no offset.
    const ZONE_FILE: &[u8] = b"TZif\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\
        \0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\x01\0\0\0\x04\
        \0\0\0\0\0\0UTC\0";

    fn zones_under(scratch: &Scratch, roots: &[&str]) -> SystemZones {
        SystemZones {
            roots: roots.iter().map(|root| scratch.path(root)).collect(),
        }
    }

    #[test]
    fn the_host_searches_the_four_python_directories() {
        let roots: Vec<&Path> = [
            "/usr/share/zoneinfo",
            "/usr/lib/zoneinfo",
            "/usr/share/lib/zoneinfo",
            "/etc/zoneinfo",
        ]
        .iter()
        .map(Path::new)
        .collect();

        assert_eq!(SystemZones::host().roots, roots);
    }

    #[test]
    fn a_zone_is_a_file_that_starts_as_a_zone_file_starts() {
        let scratch = Scratch::new();
        scratch.write("first/Area/Zone", ZONE_FILE);
        scratch.write("first/Table", &[b'x'; 64]);
        scratch.write("first/Empty", b"");
        scratch.write("first/Short", b"TZif");
        scratch.write("first/Folder/Zone", ZONE_FILE);
        let zones = zones_under(&scratch, &["first"]);

        assert!(zones.knows("Area/Zone"));
        for key in [
            "Table",
            "Empty",
            "Short",
            "Folder",
            "Area",
            "Missing",
            "Area/Missing",
        ] {
            assert!(!zones.knows(key), "{key}");
        }
    }

    #[test]
    fn a_key_that_leaves_the_directory_names_no_zone() {
        let scratch = Scratch::new();
        scratch.write("first/Zone", ZONE_FILE);
        scratch.write("outside", ZONE_FILE);
        let zones = zones_under(&scratch, &["first"]);
        let absolute = scratch.path("first/Zone");

        assert!(zones.knows("Zone"));
        for key in [
            "../outside",
            "../first/Zone",
            "./Zone",
            absolute.to_str().unwrap(),
        ] {
            assert!(!zones.knows(key), "{key}");
        }
    }

    #[test]
    fn the_first_file_of_the_search_decides() {
        let scratch = Scratch::new();
        scratch.write("first/Shadowed", &[b'x'; 64]);
        scratch.write("second/Shadowed", ZONE_FILE);
        scratch.write("first/Folder/inside", b"");
        scratch.write("second/Folder", ZONE_FILE);
        scratch.write("second/Later", ZONE_FILE);

        let zones = zones_under(&scratch, &["first", "second"]);
        assert!(!zones.knows("Shadowed"));
        assert!(zones.knows("Folder"));
        assert!(zones.knows("Later"));
        assert!(zones_under(&scratch, &["second", "first"]).knows("Shadowed"));
    }

    #[test]
    fn a_key_is_a_plain_relative_path() {
        for key in ["UTC", "Etc/UTC", "Etc/GMT+1"] {
            assert!(is_plain_key(key), "{key}");
        }

        let refused = [
            "",
            "/etc/passwd",
            "../zone",
            "Etc/../UTC",
            "Etc//UTC",
            "Etc/",
            "./UTC",
            "Etc/./UTC",
            "UTC\0",
        ];
        for key in refused {
            assert!(!is_plain_key(key), "{key:?}");
        }
    }
}
