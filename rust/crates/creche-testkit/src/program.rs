//! A script that stands in for a child program.
//!
//! A test of a command runner starts a real program. [`write_program`] makes
//! that program: a shell script in the directory of the test.
//!
//! The function has no Python origin. The Python tests of a child program
//! replace `subprocess.run` with a fake (`caregiver/tests/test_driver.py:23-52`),
//! and `runner::FakeRunner` is the port of that fake.

use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};

use crate::root::TempRoot;

/// The shell that writes the file, and that reads the script.
const SHELL: &str = "/bin/sh";

/// The first line of each script: the operating system starts the file with
/// [`SHELL`].
const FIRST_LINE: &str = "#!/bin/sh\n";

/// The command of the shell that writes the file. `$1` is the path and `$2`
/// is the text. `set -C` makes the shell refuse a path that exists.
const WRITE: &str = r#"set -C; printf '%s' "$2" > "$1""#;

/// The mode of the script: only the owner reads, writes and starts it.
const OWNER_ONLY: u32 = 0o700;

/// Writes the shell script `script` as the file `name` under `root`, with
/// mode `0700`, and returns its path.
///
/// A test of a command runner starts the script in place of a real program.
/// The function has no Python origin.
///
/// The file holds the line `#!/bin/sh` and then `script`. The function adds
/// that line to each script, so `script` holds only the commands.
///
/// `name` is one file name: it holds no `/`, and it is not `.` or `..`. The
/// function does not replace a file.
///
/// A shell writes the file, and this process never opens it. The reason:
/// Linux refuses to start a file that a process holds open for a write, with
/// the error `ETXTBSY`. A test process starts children from more than one
/// thread. A child that starts while this process holds the file open
/// inherits the open file until its own start completes. The start of the
/// script then fails now and then. With the shell, only the shell holds the
/// file, and the shell ended before this function returns.
///
/// The script goes to the shell as one argument. The operating system thus
/// refuses a script with a NUL byte and a script of about 128 KiB or more.
///
/// ```
/// use creche_testkit::program::write_program;
/// use creche_testkit::root::TempRoot;
///
/// let root = TempRoot::new()?;
/// let program = write_program(&root, "greet", "echo \"hello $1\"\n")?;
/// let output = std::process::Command::new(&program).arg("chat").output()?;
///
/// assert_eq!(program, root.path().join("greet"));
/// assert_eq!(output.stdout, b"hello chat\n");
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// # Errors
///
/// - `InvalidInput` for a `name` that is not one file name.
/// - `AlreadyExists` for a file that exists.
/// - The error of the operating system when it cannot start the shell or
///   cannot set the mode.
/// - An error with the text of the shell when the shell cannot write the
///   file.
pub fn write_program(root: &TempRoot, name: &str, script: &str) -> io::Result<PathBuf> {
    if !is_file_name(name) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "the name of a program is one file name",
        ));
    }

    let path = root.path().join(name);
    if path.symlink_metadata().is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "a file with the name of the program exists",
        ));
    }

    let written = Command::new(SHELL)
        .args(["-c", WRITE, SHELL])
        .arg(&path)
        .arg(format!("{FIRST_LINE}{script}"))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()?;
    if !written.status.success() {
        return Err(io::Error::other(format!(
            "the shell wrote no program: {}",
            String::from_utf8_lossy(&written.stderr).trim()
        )));
    }

    fs::set_permissions(&path, fs::Permissions::from_mode(OWNER_ONLY))?;

    Ok(path)
}

/// Whether `name` is the name of one file in a directory.
fn is_file_name(name: &str) -> bool {
    !name.is_empty() && name != "." && name != ".." && !name.contains(['/', '\0'])
}

#[cfg(test)]
mod tests {
    use std::process::Output;

    use super::*;

    fn run(program: &PathBuf, arguments: &[&str]) -> Output {
        Command::new(program).args(arguments).output().unwrap()
    }

    #[test]
    fn the_program_is_a_file_under_the_root_with_mode_0700() {
        let root = TempRoot::new().unwrap();
        let program = write_program(&root, "sbx", "exit 0\n").unwrap();
        let metadata = fs::symlink_metadata(&program).unwrap();

        assert_eq!(program, root.path().join("sbx"));
        assert!(metadata.is_file());
        assert_eq!(metadata.permissions().mode() & 0o7777, 0o700);
    }

    #[test]
    fn the_file_holds_the_shell_line_and_then_the_script() {
        let root = TempRoot::new().unwrap();
        let script = "printf '%s\\n' \"$@\"\n# 100% of the text: \\n 'one' \"two\" $HOME `id`\n";
        let program = write_program(&root, "words", script).unwrap();

        assert_eq!(
            fs::read_to_string(&program).unwrap(),
            format!("#!/bin/sh\n{script}")
        );
    }

    #[test]
    fn the_operating_system_starts_the_program_with_its_exact_words() {
        let root = TempRoot::new().unwrap();
        let program = write_program(&root, "words", "printf '<%s>' \"$@\"\nexit 7\n").unwrap();
        let output = run(&program, &["one word", "", "--", "$HOME"]);

        assert_eq!(output.stdout, b"<one word><><--><$HOME>");
        assert_eq!(output.status.code(), Some(7));
    }

    #[test]
    fn a_script_with_no_final_newline_and_an_empty_script_run() {
        let root = TempRoot::new().unwrap();
        let no_newline = write_program(&root, "no-newline", "echo ok").unwrap();
        let empty = write_program(&root, "empty", "").unwrap();

        assert_eq!(run(&no_newline, &[]).stdout, b"ok\n");
        assert!(run(&empty, &[]).status.success());
        assert_eq!(fs::read_to_string(&empty).unwrap(), "#!/bin/sh\n");
    }

    #[test]
    fn a_name_that_is_not_one_file_name_is_refused() {
        let root = TempRoot::new().unwrap();
        // A name that only this process uses. Each test process shares the
        // directory above the root, so a fixed name there can be the file of
        // another run.
        let escape = format!("ct-escape-{}", std::process::id());
        let outside = root.path().parent().unwrap().join(&escape);
        let above = format!("../{escape}");
        let absolute = outside.to_str().unwrap().to_owned();
        let names = [
            "",
            ".",
            "..",
            above.as_str(),
            "bin/sbx",
            absolute.as_str(),
            "sbx/",
            "s\0bx",
        ];

        let results = names.map(|name| write_program(&root, name, "exit 0\n"));
        // A function that takes a name with a `/` writes this file. The test
        // removes it before the first assertion, so a failure leaves no file
        // after the run.
        let escaped = outside.symlink_metadata().is_ok();
        let _ = fs::remove_file(&outside);

        for (name, result) in names.iter().zip(results) {
            let error = result.unwrap_err();

            assert_eq!(error.kind(), io::ErrorKind::InvalidInput, "{name:?}");
        }

        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
        assert!(!escaped, "{}", outside.display());
    }

    #[test]
    fn a_name_with_a_dot_or_a_minus_is_a_file_name() {
        let root = TempRoot::new().unwrap();

        for name in ["sbx.sh", ".hidden", "-n", "a b"] {
            let program = write_program(&root, name, "echo ok\n").unwrap();

            assert_eq!(run(&program, &[]).stdout, b"ok\n", "{name}");
        }
    }

    #[test]
    fn the_function_does_not_replace_a_file() {
        let root = TempRoot::new().unwrap();
        let program = write_program(&root, "sbx", "echo first\n").unwrap();
        let second = write_program(&root, "sbx", "echo second\n").unwrap_err();
        fs::create_dir(root.path().join("dir")).unwrap();
        let on_a_dir = write_program(&root, "dir", "echo third\n").unwrap_err();
        std::os::unix::fs::symlink("absent", root.path().join("link")).unwrap();
        let on_a_link = write_program(&root, "link", "echo fourth\n").unwrap_err();

        assert_eq!(second.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(on_a_dir.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(on_a_link.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(run(&program, &[]).stdout, b"first\n");
        assert!(!root.path().join("absent").exists());
    }

    #[test]
    fn a_script_with_a_nul_byte_is_an_error_and_leaves_no_file() {
        let root = TempRoot::new().unwrap();
        let error = write_program(&root, "sbx", "echo a\0b\n").unwrap_err();

        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }

    #[test]
    fn a_root_that_is_gone_is_an_error_with_the_text_of_the_shell() {
        let root = TempRoot::new().unwrap();
        fs::remove_dir(root.path()).unwrap();
        let error = write_program(&root, "sbx", "exit 0\n").unwrap_err();

        assert!(
            error
                .to_string()
                .starts_with("the shell wrote no program: "),
            "{error}"
        );
        assert!(error.to_string().len() > "the shell wrote no program: ".len());
        assert!(!root.path().exists());
    }

    /// Four threads write a program and start it at once, 25 times each.
    /// With a write from this process, a start fails now and then on Linux
    /// with `ETXTBSY`: the child of another thread still holds the file open.
    #[test]
    fn a_program_starts_at_once_while_other_threads_start_children() {
        const THREADS: usize = 4;
        const PROGRAMS: usize = 25;

        let root = TempRoot::new().unwrap();
        let root = &root;

        std::thread::scope(|scope| {
            for thread in 0..THREADS {
                scope.spawn(move || {
                    for number in 0..PROGRAMS {
                        let name = format!("p-{thread}-{number}");
                        let program = write_program(root, &name, "echo \"$0\"\n").unwrap();
                        let output = Command::new(&program).output().unwrap();

                        assert_eq!(
                            output.stdout,
                            format!("{}\n", program.display()).into_bytes()
                        );
                    }
                });
            }
        });
    }
}
