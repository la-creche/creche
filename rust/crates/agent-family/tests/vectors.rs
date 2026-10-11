//! The differential test against the Python package `agent_family`.
//!
//! The vector files hold what the Python code does with each input. The crate
//! `creche-vectors` reads them. Each test here walks one surface, gives each
//! input to the Rust code and compares: the result, each issue with its order
//! and its message, the parsed value field by field, and for the program each
//! byte of its output.

#[cfg(test)]
mod walk {
    use std::collections::BTreeMap;
    use std::fs;
    use std::path::PathBuf;
    use std::process::Command;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use agent_family::{
        Diff, HostFacts, Registry, Report, SystemZones, classify, load_registry, parse_family,
    };
    use creche_contracts::family::RawFamily;
    use creche_contracts::server::RawServer;
    use creche_vectors::{self as vectors, Outcome, RegistryFile, Vector};
    use serde_json::{Map, Value, json};

    /// The key of the issues of a report in a vector.
    const ISSUES_KEY: &str = "issues";

    /// The key of the state of a report in a vector.
    const STATUS_KEY: &str = "status";

    /// The start of the name of each surface of this crate.
    const OWN_SURFACES: [&str; 2] = ["family_file", "server_file"];

    /// Each surface that a test of this file walks.
    const SURFACES: [&str; 5] = [
        "family_file",
        "family_file.host",
        "server_file",
        "family_file.classify",
        "family_file.cli",
    ];

    /// What the Rust report of one vector holds that the Python report does
    /// not hold.
    enum Difference {
        /// The Python code accepts the file. The Rust report holds one error
        /// for the whole file, with this message.
        Refused(&'static str),
        /// The Python code refuses the file with other issues. The Rust
        /// report holds one error for the whole file, with this message.
        OtherRefusal(&'static str),
    }

    /// One difference from the Python code that this crate has on purpose.
    struct Deviation {
        surface: &'static str,
        vector: &'static str,
        /// The contract section that the decision reads.
        contract: &'static str,
        difference: Difference,
    }

    /// A row for a text that has no value. The Python reader gives one
    /// message for each such text. The Rust reader gives the cause, in
    /// `message`.
    const fn no_value(vector: &'static str, message: &'static str) -> Deviation {
        Deviation {
            surface: "family_file",
            vector,
            contract: "contract 01 §7: no message for a text that has no value",
            difference: Difference::OtherRefusal(message),
        }
    }

    /// Each vector on which the Rust code differs from the Python code on
    /// purpose.
    const DEVIATIONS: [Deviation; 10] = [
        no_value(
            "yaml-huge-int",
            "YAML will not parse: an integer has more than 4300 digits",
        ),
        no_value(
            "yaml-huge-hex-int",
            "YAML will not parse: an integer has more than 4300 digits",
        ),
        no_value(
            "yaml-date-no-day",
            "YAML will not parse: a value with the tag timestamp is no date and no time",
        ),
        no_value(
            "yaml-tag-int-empty",
            "YAML will not parse: an integer has no digit",
        ),
        no_value(
            "yaml-tag-bool-word",
            "YAML will not parse: a value with the tag bool is not a boolean word",
        ),
        no_value(
            "yaml-tag-timestamp-word",
            "YAML will not parse: a value with the tag timestamp is no date and no time",
        ),
        no_value(
            "yaml-sexagesimal-float-huge",
            "YAML will not parse: a base 60 float has too many parts",
        ),
        Deviation {
            surface: "family_file",
            vector: "yaml-deep-flow",
            contract: "contract 01 §1 and §2: no limit for the nesting of a file",
            // The Python reader refuses a text that nests deeper than its
            // interpreter follows. The Rust reader stops at 128 levels.
            difference: Difference::OtherRefusal(
                "YAML will not parse: the text nests deeper than 128 levels",
            ),
        },
        Deviation {
            surface: "family_file",
            vector: "yaml-escape-surrogate",
            contract: "contract 01 §1: the file is UTF-8 text",
            // The Python reader makes a string with one lone surrogate from
            // the escape `\ud800` and accepts the file. A Rust `String`
            // cannot hold that value, so the Rust reader refuses the file.
            difference: Difference::Refused(
                "YAML will not parse: the escape for the surrogate U+D800 gives no character",
            ),
        },
        Deviation {
            surface: "family_file",
            vector: "nest-129-levels",
            contract: "contract 01 §1 and §2: no limit for the nesting of a file",
            // The Python code reads the text and refuses the type of the
            // field. The Rust reader stops at 128 levels, so that a text
            // cannot use up the stack of its caller.
            difference: Difference::OtherRefusal(
                "YAML will not parse: the text nests deeper than 128 levels",
            ),
        },
    ];

    fn deviation(surface: &str, vector: &str) -> Option<&'static Deviation> {
        DEVIATIONS
            .iter()
            .find(|row| row.surface == surface && row.vector == vector)
    }

    /// Each file of each registry of the repository that a vector names, by
    /// the path of its registry.
    fn registries() -> BTreeMap<String, Vec<RegistryFile>> {
        let mut found: BTreeMap<String, Vec<RegistryFile>> = BTreeMap::new();
        for file in vectors::registries().unwrap() {
            found
                .entry(file.registry().to_owned())
                .or_default()
                .push(file);
        }

        found
    }

    /// A directory that one vector fills, and that goes away with the value.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            static COUNT: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "agent-family-vectors-{}-{}",
                std::process::id(),
                COUNT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();

            Self(path)
        }

        fn write(&self, path: &str, bytes: &[u8]) {
            let target = self.0.join(path);
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            fs::write(target, bytes).unwrap();
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    /// A copy of the registry of a vector under `prefix` in a new directory,
    /// with each file of `params.files`.
    fn staged(
        registries: &BTreeMap<String, Vec<RegistryFile>>,
        params: &Value,
        prefix: &str,
    ) -> Scratch {
        let scratch = Scratch::new();
        let registry = params["registry"].as_str().unwrap();
        for file in &registries[registry] {
            scratch.write(&format!("{prefix}{}", file.path()), file.bytes());
        }

        for (path, text) in params["files"].as_object().into_iter().flatten() {
            scratch.write(
                &format!("{prefix}{path}"),
                text.as_str().unwrap().as_bytes(),
            );
        }

        scratch
    }

    /// The host that the generator wrote: `context.host` of a vector file.
    struct WrittenHost {
        aliases: Vec<String>,
        links: BTreeMap<String, String>,
    }

    impl WrittenHost {
        fn of(context: &Map<String, Value>) -> Option<Self> {
            let host = context.get("host")?;
            let aliases = host["model_aliases"]
                .as_array()?
                .iter()
                .map(|alias| alias.as_str().unwrap().to_owned())
                .collect();
            let links = host["links"]
                .as_object()?
                .iter()
                .map(|(from, to)| (from.clone(), to.as_str().unwrap().to_owned()))
                .collect();

            Some(Self { aliases, links })
        }
    }

    impl HostFacts for WrittenHost {
        fn serves_alias(&self, alias: &str) -> bool {
            self.aliases.iter().any(|one| one == alias)
        }

        fn real_path(&self, path: &str) -> String {
            self.links
                .get(path)
                .cloned()
                .unwrap_or_else(|| path.to_owned())
        }
    }

    /// How far the Rust code takes a file that the Python code accepts.
    #[derive(Debug, PartialEq, Eq)]
    enum Reach {
        /// The file has a valid value, as in Python.
        Valid,
        /// The file has no value.
        Refused,
    }

    fn reach(surface: &str, vector: &str) -> Reach {
        match deviation(surface, vector).map(|row| &row.difference) {
            Some(Difference::Refused(_)) => Reach::Refused,
            _ => Reach::Valid,
        }
    }

    fn error_json(loc: &str, msg: &str) -> Value {
        json!({"severity": "error", "loc": loc, "msg": msg, "downgraded": false})
    }

    /// The state of a report, as a vector holds it under the key `status`.
    fn state_json(report: &Report) -> Value {
        json!(report.state().as_str())
    }

    /// Compares one report with one vector. For a vector with a deviation
    /// row, the report must hold the difference that the row names.
    fn check_report(surface: &str, vector: &Vector, report: &Report) {
        let id = vector.id();
        let issues = serde_json::to_value(report.issues()).unwrap();
        match deviation(surface, id).map(|row| (&row.difference, row.contract)) {
            Some((Difference::Refused(msg), contract)) => {
                assert_eq!(vector.result(), Outcome::Accepted, "{id}: {contract}");
                assert_eq!(
                    issues,
                    json!([error_json("<document>", msg)]),
                    "{id}: the deviation row names no difference"
                );

                return;
            }
            Some((Difference::OtherRefusal(msg), contract)) => {
                let wanted = json!([error_json("<document>", msg)]);
                assert_eq!(vector.result(), Outcome::Refused, "{id}: {contract}");
                assert_ne!(
                    vector.field(ISSUES_KEY),
                    Some(&wanted),
                    "{id}: the deviation row names no difference"
                );
                assert_eq!(issues, wanted, "{id}: {contract}");
                assert_eq!(Some(&state_json(report)), vector.field(STATUS_KEY), "{id}");

                return;
            }
            None => {}
        }

        assert_eq!(Some(&issues), vector.field(ISSUES_KEY), "{surface} {id}");
        assert_eq!(
            Some(&state_json(report)),
            vector.field(STATUS_KEY),
            "{surface} {id}"
        );
        assert_eq!(
            if report.ok() {
                Outcome::Accepted
            } else {
                Outcome::Refused
            },
            vector.result(),
            "{surface} {id}"
        );
    }

    fn walk_family_file(surface_name: &str) {
        let surface = vectors::surface(surface_name).unwrap();
        let host = WrittenHost::of(surface.context());
        let host: Option<&dyn HostFacts> = host.as_ref().map(|host| -> &dyn HostFacts { host });
        let registries = registries();
        for vector in surface.vectors() {
            let id = vector.id();
            let params = vector.params().unwrap();
            let directory = params["directory"].as_str().unwrap();
            let scratch = staged(&registries, params, "");
            scratch.write(
                &format!("families/{directory}/family.yaml"),
                &vector.input().bytes().unwrap(),
            );
            let loaded: Registry = load_registry(&scratch.0, host, &SystemZones::host());
            let report = &loaded.reports()[directory];
            check_report(surface_name, vector, report);
            let reach = reach(surface_name, id);
            if vector.result() != Outcome::Accepted || reach == Reach::Refused {
                assert!(!loaded.families().contains_key(directory), "{id}");
                continue;
            }

            // The parsed file, and the valid family, hold each field as the
            // Python model holds it.
            let parsed = &loaded.parsed_families()[directory];
            assert_eq!(
                Some(&serde_json::to_value(parsed).unwrap()),
                vector.value(),
                "{id}"
            );
            let family = &loaded.families()[directory];
            assert_eq!(
                Some(&serde_json::to_value(RawFamily::from(family)).unwrap()),
                vector.value(),
                "{id}"
            );
        }
    }

    #[test]
    fn a_family_file_has_the_python_report() {
        walk_family_file("family_file");
    }

    #[test]
    fn a_family_file_on_a_host_has_the_python_report() {
        walk_family_file("family_file.host");
    }

    #[test]
    fn a_server_file_has_the_python_report() {
        let surface = vectors::surface("server_file").unwrap();
        let registries = registries();
        for vector in surface.vectors() {
            let id = vector.id();
            let params = vector.params().unwrap();
            let directory = params["directory"].as_str().unwrap();
            let scratch = staged(&registries, params, "");
            scratch.write(
                &format!("mcp/{directory}/server.yaml"),
                &vector.input().bytes().unwrap(),
            );
            let loaded = load_registry(&scratch.0, None, &SystemZones::host());
            let report = &loaded.server_reports()[directory];
            check_report("server_file", vector, report);
            if vector.result() != Outcome::Accepted {
                assert!(!loaded.servers().contains_key(directory), "{id}");
                continue;
            }

            let parsed = &loaded.parsed_servers()[directory];
            assert_eq!(
                Some(&serde_json::to_value(parsed).unwrap()),
                vector.value(),
                "{id}"
            );
            let server = &loaded.servers()[directory];
            assert_eq!(
                Some(&serde_json::to_value(RawServer::from(server)).unwrap()),
                vector.value(),
                "{id}"
            );
        }
    }

    /// The JSON form of a diff that the generator writes.
    fn diff_value(diff: &Diff) -> Value {
        let changes: Vec<Value> = diff
            .changes()
            .iter()
            .map(|change| {
                json!({
                    "field": change.field,
                    "landing": change.landing.as_str(),
                    "direction": change.direction.as_str(),
                    "step": change.step.as_str(),
                    "detail": change.detail,
                })
            })
            .collect();
        let refused: Vec<&str> = diff.refused().iter().map(|change| change.field).collect();
        let steps: Vec<&str> = diff.steps().iter().map(|step| step.as_str()).collect();

        json!({
            "changes": changes,
            "changed": diff.changed(),
            "refused": refused,
            "needs_switch": diff.needs_switch(),
            "switch_mode": diff.switch_mode().map(|mode| mode.as_str()),
            "steps": steps,
            "reason": diff.reason(),
        })
    }

    #[test]
    fn a_classified_change_is_the_python_diff() {
        let surface = vectors::surface("family_file.classify").unwrap();
        for vector in surface.vectors() {
            let id = vector.id();
            let args = vector.input().args().unwrap();
            let parse = |key: &str| {
                let text = args[key].as_str().unwrap();
                let (raw, issues) = parse_family(text);

                raw.unwrap_or_else(|| panic!("{id} {key}: {issues:?}"))
            };
            let diff = classify(&parse("old"), &parse("new"));
            assert_eq!(vector.result(), Outcome::Accepted, "{id}");
            assert_eq!(Some(&diff_value(&diff)), vector.value(), "{id}");
        }
    }

    #[test]
    fn the_program_writes_the_python_output() {
        let surface = vectors::surface("family_file.cli").unwrap();
        let registries = registries();
        for vector in surface.vectors() {
            let id = vector.id();
            let accepted = vector.result() == Outcome::Accepted;
            let scratch = staged(&registries, vector.params().unwrap(), "registry/");
            let argv: Vec<&str> = vector.input().args().unwrap()["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|arg| arg.as_str().unwrap())
                .collect();
            let output = Command::new(env!("CARGO_BIN_EXE_agent-family"))
                .args(&argv)
                .current_dir(&scratch.0)
                .output()
                .unwrap();
            let wanted = if accepted {
                vector.value()
            } else {
                vector.refusal()
            };
            let wanted = wanted.unwrap_or_else(|| panic!("{id}: no output of the program"));
            assert_eq!(
                output.status.code().map(i64::from),
                wanted["exit"].as_i64(),
                "{id}"
            );
            assert_eq!(wanted["exit"] == 0, accepted, "{id}");
            // A vector holds no text that argparse writes: that text differs
            // between two Python versions.
            if let Some(stdout) = wanted["stdout"].as_str() {
                assert_eq!(String::from_utf8_lossy(&output.stdout), stdout, "{id}");
            }

            if let Some(stderr) = wanted["stderr"].as_str() {
                assert_eq!(String::from_utf8_lossy(&output.stderr), stderr, "{id}");
            } else {
                assert!(!output.stderr.is_empty() || wanted["exit"] == 0, "{id}");
            }
        }
    }

    #[test]
    fn each_surface_of_this_crate_has_a_test() {
        let index = vectors::index().unwrap();
        let own: Vec<&str> = index
            .iter()
            .map(|row| row.surface())
            .filter(|name| OWN_SURFACES.iter().any(|start| name.starts_with(start)))
            .collect();
        for name in &own {
            assert!(SURFACES.contains(name), "no test walks the surface {name}");
        }

        for name in SURFACES {
            assert!(own.contains(&name), "the index holds no surface {name}");
        }
    }

    #[test]
    fn each_deviation_names_a_vector() {
        for row in &DEVIATIONS {
            let surface = vectors::surface(row.surface).unwrap();
            assert!(
                surface
                    .vectors()
                    .iter()
                    .any(|vector| vector.id() == row.vector),
                "{} holds no vector {}",
                row.surface,
                row.vector
            );
            assert!(!row.contract.is_empty());
        }
    }
}
