//! The validator of the family file and of the MCP server file (contract 01
//! and contract 01b), the registry loader and the `agent-family` program.
//!
//! The Python package `agent_family` is the authority until a release of
//! `caregiver` uses this crate. Each function here gives the answer of its
//! Python function: the same values, the same issues in the same order and
//! the same messages. The vector files under `vectors/data` hold the Python
//! answers, and the tests of this crate compare.
//!
//! | Module | Holds |
//! |---|---|
//! | `yaml` | a YAML reader that reads a text as PyYAML reads it |
//! | `shape`, `lax`, `parse` | YAML text to a raw file, or to issues |
//! | `crossref` | the rules that need the registry or the host |
//! | `registry` | the loader of a registry, and its revision |
//! | `classify` | live or replacement, field by field |
//! | `report`, `json`, `cli` | the report, and the program that prints it |

mod classify;
mod cli;
mod crossref;
mod difflib;
mod json;
mod lax;
mod parse;
mod registry;
mod report;
#[cfg(test)]
mod scratch;
mod shape;
mod yaml;
mod zones;

pub use classify::{Diff, Direction, FieldChange, Landing, Step, SwitchMode, classify};
pub use cli::{EXIT_INVALID, EXIT_OK, EXIT_USAGE, run};
pub use crossref::{HostFacts, Index, check_family, check_server};
pub use parse::{DOCUMENT, parse_family, parse_server};
pub use registry::{Registry, load_registry, revision_of};
pub use report::{Applied, FIRST_ERROR_CHARS, FamilyState, Report};
pub use zones::{SystemZones, ZoneFacts};
