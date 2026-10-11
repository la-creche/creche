//! The tests that start `agent-family` as a process.
//!
//! `agent_family::entry` sets the panic hook of its process, so no test
//! calls it in the test program. Each test here starts the built program
//! with an empty stdin. It reads only what crossed the process boundary: the
//! exit status and the two output streams.
//!
//! The differential test in `vectors.rs` holds the report of the program
//! equal to the Python report. The tests here hold the command lines that
//! end before a report: the help, a usage mistake and a word that is not
//! UTF-8 text.
//!
//! No test here makes the program panic, because no command line does that.
//! `creche-runtime` holds the test of the boundary that `entry` calls.

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::fs;
    use std::os::unix::ffi::OsStringExt;
    use std::process::{self, Command, Stdio};

    use agent_family::{EXIT_OK, EXIT_USAGE};

    /// The built program. cargo builds it before this test program.
    const PROGRAM: &str = env!("CARGO_BIN_EXE_agent-family");

    /// The start of the usage text and of the help text.
    const USAGE: &str = "usage: agent-family";

    /// What one run of the program gave across the process boundary.
    struct Ended {
        status: Option<i32>,
        stdout: String,
        stderr: String,
    }

    /// Starts the program with `words` and an empty stdin, and waits for its
    /// end.
    fn run_program(words: &[OsString]) -> Ended {
        let output = Command::new(PROGRAM)
            .args(words)
            .stdin(Stdio::null())
            .output()
            .unwrap();

        Ended {
            status: output.status.code(),
            stdout: String::from_utf8(output.stdout).unwrap(),
            stderr: String::from_utf8(output.stderr).unwrap(),
        }
    }

    fn words(line: &[&str]) -> Vec<OsString> {
        line.iter().map(OsString::from).collect()
    }

    #[test]
    fn the_help_flag_writes_the_help_to_stdout() {
        let ended = run_program(&words(&["--help"]));

        assert_eq!(ended.status, Some(i32::from(EXIT_OK)));
        assert!(ended.stdout.starts_with(USAGE), "{}", ended.stdout);
        assert_eq!(ended.stderr, "");
    }

    #[test]
    fn no_argument_is_a_usage_mistake() {
        let ended = run_program(&[]);

        assert_eq!(ended.status, Some(i32::from(EXIT_USAGE)));
        assert_eq!(ended.stdout, "");
        assert!(ended.stderr.starts_with(USAGE), "{}", ended.stderr);
    }

    /// The path names no file: the test makes none. The name holds the id of
    /// this test process, so no other run of the tests names the same path.
    #[test]
    fn a_path_that_is_no_directory_is_a_usage_mistake() {
        // The program writes a path with no empty part and no `.` part. The
        // real path of the temporary directory has that form.
        let absent = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("agent-family-process-{}-absent", process::id()))
            .into_os_string()
            .into_string()
            .unwrap();
        let ended = run_program(&words(&["validate", &absent]));

        assert_eq!(ended.status, Some(i32::from(EXIT_USAGE)));
        assert_eq!(ended.stdout, "");
        assert_eq!(
            ended.stderr,
            format!("agent-family: {absent} is not a directory\n")
        );
    }

    #[test]
    fn a_word_that_is_not_utf8_is_a_usage_mistake() {
        let ended = run_program(&[OsString::from_vec(vec![0xff])]);

        assert_eq!(ended.status, Some(i32::from(EXIT_USAGE)));
        assert_eq!(ended.stdout, "");
        assert_eq!(
            ended.stderr,
            "agent-family: an argument is not UTF-8 text\n"
        );
    }
}
