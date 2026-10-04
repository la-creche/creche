//! `agent-family validate <registry path> [--family NAME] [--json]`.
//!
//! The program exits with 0 when each report is clean, with 1 when a report
//! holds an error and with 2 for a usage mistake. The report is the product
//! (invariant 19), so the program prints it in both cases.
//!
//! The Python program reads its arguments with `argparse`. This module reads
//! the same command lines and writes the same text, as Python 3.13 writes it.

use std::io::Write;
use std::path::Path;

use creche_contracts::family::Severity;

use crate::json;
use crate::registry::load_registry;
use crate::report::Report;
use crate::zones::SystemZones;

/// Each report is clean.
pub const EXIT_OK: u8 = 0;
/// A report holds an error.
pub const EXIT_INVALID: u8 = 1;
/// The command line is wrong.
pub const EXIT_USAGE: u8 = 2;

const PROG: &str = "agent-family";
const COMMAND: &str = "validate";

const MAIN_USAGE: &str = "usage: agent-family [-h] {validate} ...";
const VALIDATE_USAGE: &str =
    "usage: agent-family validate [-h] [--family FAMILY] [--json] registry";

const MAIN_HELP: &str = "\
usage: agent-family [-h] {validate} ...

Validate a family registry (docs/rework/contracts/01).

positional arguments:
  {validate}
    validate  read a registry and print one report per family

options:
  -h, --help  show this help message and exit
";

const VALIDATE_HELP: &str = "\
usage: agent-family validate [-h] [--family FAMILY] [--json] registry

positional arguments:
  registry         the registry root, holding families/

options:
  -h, --help       show this help message and exit
  --family FAMILY  report this family only
  --json           print the report as JSON
";

/// How the program prints the reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Format {
    Text,
    Json,
}

/// What a command line asks for.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Asked {
    Validate {
        registry: String,
        family: Option<String>,
        format: Format,
    },
    /// Print this text and exit with 0.
    Help(&'static str),
    /// Print this text to the error stream and exit with 2.
    Usage(String),
}

fn main_error(msg: &str) -> Asked {
    Asked::Usage(format!("{MAIN_USAGE}\n{PROG}: error: {msg}\n"))
}

fn validate_error(msg: &str) -> Asked {
    Asked::Usage(format!(
        "{VALIDATE_USAGE}\n{PROG} {COMMAND}: error: {msg}\n"
    ))
}

/// The options of the program, in the order of their declaration.
const MAIN_OPTIONS: [&str; 2] = ["-h", "--help"];

/// The options of `validate`, in the order of their declaration.
const VALIDATE_OPTIONS: [&str; 4] = ["-h", "--help", "--family", "--json"];

/// The mark that ends the options: each later argument is a value.
const END_OF_OPTIONS: &str = "--";

/// How `argparse` of Python 3.13 reads one argument.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Read<'a> {
    /// A value: the command, the registry or the value of an option.
    Value,
    /// `-h`, and what follows it in the same argument.
    ShortHelp(&'a str),
    /// A long option, by its name or by the start of its name, and the text
    /// after `=`.
    Long(&'static str, Option<&'a str>),
    /// The start of more than one option: each such option.
    Ambiguous(Vec<&'static str>),
    /// An option that the parser does not have.
    Unknown,
}

/// Whether `argparse` of Python 3.13 reads `arg` as a negative number:
/// `-5`, `-5.5` and `-.5`, and not `-5.`.
fn is_negative_number(arg: &str) -> bool {
    // The `$` of a Python pattern also matches before one final newline.
    let arg = arg.strip_suffix('\n').unwrap_or(arg);
    let Some(rest) = arg.strip_prefix('-') else {
        return false;
    };

    let digits = |text: &str| !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit());
    match rest.split_once('.') {
        None => digits(rest),
        Some((whole, fraction)) => (whole.is_empty() || digits(whole)) && digits(fraction),
    }
}

/// `_parse_optional` of `argparse`: what one argument is for a parser with
/// the options `options`.
fn read_arg<'a>(arg: &'a str, options: &[&'static str]) -> Read<'a> {
    if !arg.starts_with('-') || arg.len() == 1 {
        return Read::Value;
    }

    let (name, explicit) = match arg.split_once('=') {
        Some((name, value)) => (name, Some(value)),
        None => (arg, None),
    };
    let long = arg.starts_with("--");
    if let Some(exact) = options.iter().find(|option| **option == name) {
        return match arg.strip_prefix("-h").filter(|_| !long) {
            Some(rest) => Read::ShortHelp(rest),
            None => Read::Long(exact, explicit),
        };
    }

    // The start of a name stands for the name. One dash and one letter also
    // stand for the short option with that letter.
    let short = arg.get(..2).filter(|_| !long);
    let near: Vec<&'static str> = options
        .iter()
        .filter(|option| Some(**option) == short || option.starts_with(name))
        .copied()
        .collect();
    match near.as_slice() {
        [] => {}
        ["-h"] => return Read::ShortHelp(arg.strip_prefix("-h").unwrap_or_default()),
        [one] => return Read::Long(one, explicit),
        _ => return Read::Ambiguous(near),
    }

    // A negative number and a text with a space are values.
    if is_negative_number(arg) || arg.contains(' ') {
        return Read::Value;
    }

    Read::Unknown
}

/// What `-h` with the text `rest` after it in one argument asks for: the
/// help, or the error for a value that the option does not take. The `Err`
/// holds that value.
fn short_help(rest: &str) -> Result<(), &str> {
    let mut rest = rest;
    loop {
        if let Some(value) = rest.strip_prefix('=') {
            return Err(value);
        }

        if rest.starts_with('-') {
            return Err(rest);
        }

        // One more `h` is one more `-h`. Another letter is another option,
        // and the help comes first.
        match rest.strip_prefix('h') {
            Some(after) => rest = after,
            None => return Ok(()),
        }
    }
}

fn ignored(value: &str) -> String {
    format!(
        "argument -h/--help: ignored explicit argument {}",
        crate::yaml::text::repr_str(value)
    )
}

fn ambiguous(arg: &str, options: &[&str]) -> String {
    format!("ambiguous option: {arg} could match {}", options.join(", "))
}

/// The index of the command, or the error for an argument in its place that
/// is not the command.
fn command_at(args: &[String], index: usize) -> Result<usize, Asked> {
    let Some(arg) = args.get(index) else {
        return Err(main_error("the following arguments are required: command"));
    };

    if arg != COMMAND {
        let said = crate::yaml::text::repr_str(arg);

        return Err(main_error(&format!(
            "argument command: invalid choice: {said} (choose from '{COMMAND}')"
        )));
    }

    Ok(index)
}

/// The arguments before the command. The answer is the index of the
/// command, or what the program does with no command.
fn parse_main(args: &[String], unrecognized: &mut Vec<String>) -> Result<usize, Asked> {
    for (index, arg) in args.iter().enumerate() {
        // After the mark, the next argument is the command.
        if arg == END_OF_OPTIONS {
            return command_at(args, index + 1);
        }

        match read_arg(arg, &MAIN_OPTIONS) {
            Read::Value => return command_at(args, index),
            Read::ShortHelp(rest) => {
                return Err(match short_help(rest) {
                    Ok(()) => Asked::Help(MAIN_HELP),
                    Err(value) => main_error(&ignored(value)),
                });
            }
            Read::Long(_, None) => return Err(Asked::Help(MAIN_HELP)),
            Read::Long(_, Some(value)) => return Err(main_error(&ignored(value))),
            Read::Ambiguous(options) => return Err(main_error(&ambiguous(arg, &options))),
            Read::Unknown => unrecognized.push(arg.clone()),
        }
    }

    Err(main_error("the following arguments are required: command"))
}

/// The arguments after the command.
fn parse_validate(args: &[String], unrecognized: &mut Vec<String>) -> Asked {
    let mut registry: Option<String> = None;
    let mut family = None;
    let mut format = Format::Text;
    let mut only_values = false;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        if !only_values && arg == END_OF_OPTIONS {
            only_values = true;
            continue;
        }

        let read = if only_values {
            Read::Value
        } else {
            read_arg(arg, &VALIDATE_OPTIONS)
        };
        match read {
            Read::Value => {
                if registry.is_some() {
                    unrecognized.push(arg.clone());
                } else {
                    registry = Some(arg.clone());
                }
            }
            Read::ShortHelp(rest) => {
                return match short_help(rest) {
                    Ok(()) => Asked::Help(VALIDATE_HELP),
                    Err(value) => validate_error(&ignored(value)),
                };
            }
            Read::Long("--help", None) => return Asked::Help(VALIDATE_HELP),
            Read::Long("--help", Some(value)) => return validate_error(&ignored(value)),
            Read::Long("--json", None) => format = Format::Json,
            Read::Long("--json", Some(value)) => {
                return validate_error(&format!(
                    "argument --json: ignored explicit argument {}",
                    crate::yaml::text::repr_str(value)
                ));
            }
            Read::Long("--family", Some(value)) => family = Some(value.to_owned()),
            Read::Long("--family", None) => {
                let value = rest.clone().next().filter(|next| {
                    *next != END_OF_OPTIONS && read_arg(next, &VALIDATE_OPTIONS) == Read::Value
                });
                let Some(value) = value else {
                    return validate_error("argument --family: expected one argument");
                };

                family = Some(value.clone());
                rest.next();
            }
            Read::Ambiguous(options) => return validate_error(&ambiguous(arg, &options)),
            Read::Long(..) | Read::Unknown => unrecognized.push(arg.clone()),
        }
    }

    let Some(registry) = registry else {
        return validate_error("the following arguments are required: registry");
    };

    Asked::Validate {
        registry,
        family,
        format,
    }
}

/// What the arguments after the program name ask for.
fn parse_args(args: &[String]) -> Asked {
    let mut unrecognized = Vec::new();
    let command = match parse_main(args, &mut unrecognized) {
        Ok(index) => index,
        Err(asked) => return asked,
    };
    let after = args.get(command + 1..).unwrap_or_default();
    let asked = parse_validate(after, &mut unrecognized);
    if matches!(asked, Asked::Validate { .. }) && !unrecognized.is_empty() {
        return main_error(&format!(
            "unrecognized arguments: {}",
            unrecognized.join(" ")
        ));
    }

    asked
}

/// The text that Python writes for a path: `str(Path(text))`.
fn python_path(text: &str) -> String {
    let root = if text.starts_with("//") && !text.starts_with("///") {
        "//"
    } else if text.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = text
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    let path = format!("{root}{}", parts.join("/"));
    if path.is_empty() {
        return ".".to_owned();
    }

    path
}

fn text_report(reports: &[&Report]) -> String {
    let mut out = String::new();
    for report in reports {
        let mark = if report.ok() { "ok" } else { "INVALID" };
        out.push_str(&format!(
            "{}: {mark} ({} errors, {} warnings)\n  {}\n",
            report.family(),
            report.errors(),
            report.warnings(),
            report.file()
        ));
        for issue in report.issues() {
            let flag = if issue.downgraded {
                " [downgraded]"
            } else {
                ""
            };
            let label = match issue.severity {
                Severity::Error => "error",
                Severity::Warning => "warning",
            };
            out.push_str(&format!("  {label}: {}: {}{flag}\n", issue.loc, issue.msg));
        }
    }

    out
}

/// Runs the program. `args` holds each argument after the program name. The
/// answer is the exit status.
///
/// At this process edge the failure action is the exit status: 2 for a
/// command line that the program cannot read and for a path that is no
/// directory, and 1 for a registry that holds an error.
pub fn run(args: &[String], out: &mut impl Write, err: &mut impl Write) -> u8 {
    let (registry, family, format) = match parse_args(args) {
        Asked::Validate {
            registry,
            family,
            format,
        } => (registry, family, format),
        Asked::Help(text) => {
            return match out.write_all(text.as_bytes()) {
                Ok(()) => EXIT_OK,
                Err(_) => EXIT_INVALID,
            };
        }
        Asked::Usage(text) => {
            // The exit status says the same as the text.
            let _ = err.write_all(text.as_bytes());

            return EXIT_USAGE;
        }
    };
    // Python reads the registry as `Path(text)`. That path has no empty part,
    // and the empty text is the working directory.
    let shown = python_path(&registry);
    let root = Path::new(&shown);
    if !root.is_dir() {
        let _ = writeln!(err, "{PROG}: {shown} is not a directory");

        return EXIT_USAGE;
    }

    let loaded = load_registry(root, None, &SystemZones::host());
    let reports: Vec<&Report> = loaded
        .all_reports()
        .into_iter()
        .filter(|report| family.as_deref().is_none_or(|only| report.family() == only))
        .collect();
    if let Some(only) = &family
        && reports.is_empty()
    {
        let _ = writeln!(err, "{PROG}: no family or server named '{only}'");

        return EXIT_USAGE;
    }

    let text = match format {
        Format::Json => format!("{}\n", json::document(&shown, loaded.revision(), &reports)),
        Format::Text => text_report(&reports),
    };
    if out.write_all(text.as_bytes()).is_err() {
        return EXIT_INVALID;
    }

    if reports.iter().all(|report| report.ok()) {
        EXIT_OK
    } else {
        EXIT_INVALID
    }
}

#[cfg(test)]
mod tests {
    use super::{Asked, EXIT_OK, Format, parse_args, python_path, run};

    fn args(line: &str) -> Vec<String> {
        line.split_whitespace().map(str::to_owned).collect()
    }

    fn validate(registry: &str, family: Option<&str>, format: Format) -> Asked {
        Asked::Validate {
            registry: registry.to_owned(),
            family: family.map(str::to_owned),
            format,
        }
    }

    fn usage_of(line: &str) -> String {
        match parse_args(&args(line)) {
            Asked::Usage(text) => text,
            other => panic!("{line}: {other:?}"),
        }
    }

    #[test]
    fn a_command_line_reads_as_argparse_reads_it() {
        let cases = [
            ("validate reg", validate("reg", None, Format::Text)),
            ("validate reg --json", validate("reg", None, Format::Json)),
            ("validate --json reg", validate("reg", None, Format::Json)),
            (
                "validate reg --family chat",
                validate("reg", Some("chat"), Format::Text),
            ),
            (
                "validate --family=chat reg --j",
                validate("reg", Some("chat"), Format::Json),
            ),
            (
                "validate reg --fam chat --family code",
                validate("reg", Some("code"), Format::Text),
            ),
            ("validate -- reg", validate("reg", None, Format::Text)),
            ("validate -- --json", validate("--json", None, Format::Text)),
            ("validate -1", validate("-1", None, Format::Text)),
            ("validate -.5", validate("-.5", None, Format::Text)),
            ("validate -1.5", validate("-1.5", None, Format::Text)),
            (
                "validate reg --family -1",
                validate("reg", Some("-1"), Format::Text),
            ),
            ("-- validate reg", validate("reg", None, Format::Text)),
            (
                "-- validate reg --json",
                validate("reg", None, Format::Json),
            ),
            (
                "validate reg --family=",
                validate("reg", Some(""), Format::Text),
            ),
        ];
        for (line, wanted) in cases {
            assert_eq!(parse_args(&args(line)), wanted, "{line}");
        }
    }

    #[test]
    fn a_help_flag_asks_for_the_help_text() {
        for line in [
            "-h",
            "--help",
            "--he",
            "-h validate reg",
            "-hx",
            "--bogus -h",
        ] {
            assert!(
                matches!(parse_args(&args(line)), Asked::Help(text) if text.contains("positional arguments:\n  {validate}")),
                "{line}"
            );
        }

        for line in [
            "validate -h",
            "validate reg --help",
            "validate --h reg",
            "validate -hx",
            "validate -hh",
            "validate -hx=y",
            "validate -h --bogus",
        ] {
            assert!(
                matches!(parse_args(&args(line)), Asked::Help(text) if text.contains("registry root")),
                "{line}"
            );
        }
    }

    /// The error lines that Python 3.13 writes.
    #[test]
    fn a_wrong_command_line_gets_the_argparse_text() {
        let main = "usage: agent-family [-h] {validate} ...\nagent-family: error: ";
        let sub = "usage: agent-family validate [-h] [--family FAMILY] [--json] \
                   registry\nagent-family validate: error: ";
        let cases = [
            ("", main, "the following arguments are required: command"),
            (
                "bogus",
                main,
                "argument command: invalid choice: 'bogus' (choose from 'validate')",
            ),
            (
                "validate x --bogus",
                main,
                "unrecognized arguments: --bogus",
            ),
            ("validate a b", main, "unrecognized arguments: b"),
            (
                "--bogus validate x",
                main,
                "unrecognized arguments: --bogus",
            ),
            ("validate x -x", main, "unrecognized arguments: -x"),
            (
                "validate",
                sub,
                "the following arguments are required: registry",
            ),
            (
                "validate x --family",
                sub,
                "argument --family: expected one argument",
            ),
            (
                "validate x --family --json",
                sub,
                "argument --family: expected one argument",
            ),
            (
                "validate x --json=1",
                sub,
                "argument --json: ignored explicit argument '1'",
            ),
            // A minus, digits and a period at the end are no number.
            (
                "validate -5.",
                sub,
                "the following arguments are required: registry",
            ),
            ("validate x -1.5.5", main, "unrecognized arguments: -1.5.5"),
            (
                "validate x --family -5.",
                sub,
                "argument --family: expected one argument",
            ),
            // After the mark `--`, the next argument is the command.
            ("--", main, "the following arguments are required: command"),
            (
                "-- -- validate x",
                main,
                "argument command: invalid choice: '--' (choose from 'validate')",
            ),
            (
                "-- -h",
                main,
                "argument command: invalid choice: '-h' (choose from 'validate')",
            ),
            (
                "--help=x validate x",
                main,
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            (
                "-h=x",
                main,
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            (
                "validate -h=",
                sub,
                "argument -h/--help: ignored explicit argument ''",
            ),
            (
                "validate -h-x",
                sub,
                "argument -h/--help: ignored explicit argument '-x'",
            ),
            (
                "validate -hh=x",
                sub,
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            // The start of more than one option.
            (
                "validate x --=y",
                sub,
                "ambiguous option: --=y could match --help, --family, --json",
            ),
            (
                "validate -=y",
                sub,
                "ambiguous option: -=y could match -h, --help, --family, --json",
            ),
            ("-=y", main, "ambiguous option: -=y could match -h, --help"),
        ];
        for (line, usage, said) in cases {
            assert_eq!(usage_of(line), format!("{usage}{said}\n"), "{line}");
        }
    }

    /// An argument with a space is a value, when it starts no option.
    #[test]
    fn an_argument_with_a_space_is_a_value() {
        let line =
            |args: &[&str]| -> Vec<String> { args.iter().map(|arg| (*arg).to_owned()).collect() };

        assert_eq!(
            parse_args(&line(&["validate", "--a b"])),
            validate("--a b", None, Format::Text)
        );
        assert_eq!(
            parse_args(&line(&["validate", "reg", "--fam=a b"])),
            validate("reg", Some("a b"), Format::Text)
        );
        assert_eq!(
            parse_args(&line(&["validate", "-5\n"])),
            validate("-5\n", None, Format::Text)
        );
    }

    /// The empty path is the working directory, as `Path("")` is in Python.
    /// The working directory of this test holds no registry, so the program
    /// has no report to print.
    #[test]
    fn the_empty_path_is_the_working_directory() {
        let line = ["validate".to_owned(), String::new(), "--json".to_owned()];
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let status = run(&line, &mut out, &mut err);
        let text = String::from_utf8(out).unwrap();

        assert_eq!(status, EXIT_OK);
        assert!(text.starts_with("{\n  \"registry\": \".\","), "{text}");
        assert!(text.ends_with("\"reports\": []\n}\n"), "{text}");
        assert_eq!(err, b"");
    }

    #[test]
    fn a_path_has_the_text_that_python_writes() {
        let cases = [
            ("a/b", "a/b"),
            ("./a//b/", "a/b"),
            ("a/./b", "a/b"),
            ("a/../b", "a/../b"),
            (".", "."),
            ("", "."),
            ("/", "/"),
            ("/a/", "/a"),
            ("//a", "//a"),
            ("///a", "/a"),
        ];
        for (text, wanted) in cases {
            assert_eq!(python_path(text), wanted, "{text:?}");
        }
    }
}
