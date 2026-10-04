//! The differential test against the Python package `agent_family`.
//!
//! The vector files under `vectors/data` hold what the Python code does with
//! each input. Each test here walks one surface, gives each input to the
//! Rust code and compares: the result, each issue with its order and its
//! message, the parsed value field by field, and for the program each byte
//! of its output.

#[cfg(test)]
mod walk {
    use std::collections::BTreeMap;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use agent_family::{
        Diff, HostFacts, Registry, Report, SystemZones, classify, load_registry, parse_family,
    };
    use creche_contracts::family::RawFamily;
    use creche_contracts::server::RawServer;
    use serde_json::{Value, json};

    /// The directory of the vector files. The crate is three levels below
    /// the repository root.
    const DATA_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../vectors/data");

    /// The version of the file format that this reader takes.
    const FORMAT: u64 = 1;

    /// Each file of each registry that a vector names.
    const REGISTRIES_FILE: &str = "family_file.registries.json";

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
        /// The Python code refuses the file. The Rust report holds one issue
        /// more: this location and this message.
        OneMoreError(&'static str, &'static str),
        /// The stance `stricter` of `rust/AGENTS.md`. The Python code accepts
        /// the file. An id type of `creche_contracts::ids` refuses one text
        /// of the file, so the Rust report holds one error more: this
        /// location and this message.
        Stricter(&'static str, &'static str),
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

    /// The error for the tool name of the two vectors `long-tool-name`. The
    /// name has 65 characters.
    const LONG_TOOL_ERROR: &str = "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
                                   is not a tool name; a tool name has 64 bytes or less";

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
    const DEVIATIONS: [Deviation; 17] = [
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
            vector: "rule-egress-edges",
            contract: "contract 01 §3.7: a port is 1 to 65535",
            // The Python code reads the Arabic-Indic digit one as the port 1.
            // `rust/AGENTS.md` refuses a digit that is not ASCII.
            difference: Difference::OneMoreError(
                "egress[11]",
                "'example.com:\u{661}' has a port outside 1 to 65535",
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
        Deviation {
            surface: "family_file",
            vector: "long-tool-name",
            contract: "contract 01 §3.4: no limit for the size of a tool name",
            // `agent_family` has no limit. `ids::ToolName` takes the limit
            // of 64 bytes of the strictest Python copy.
            difference: Difference::Stricter("tools.long-tool-server[0]", LONG_TOOL_ERROR),
        },
        Deviation {
            surface: "server_file",
            vector: "long-tool-name",
            contract: "contract 01b §5: no limit for the size of a tool name",
            difference: Difference::Stricter("tools[0]", LONG_TOOL_ERROR),
        },
        Deviation {
            surface: "server_file",
            vector: "long-version-hyphen",
            contract: "contract 01b §3.1: no grammar for a version",
            // `agent_family` permits `+` and `-` in a version and has no
            // limit. `ids::PackageVersion` takes the strictest Python copy:
            // no `+`, no `-` and 64 bytes at most.
            difference: Difference::Stricter(
                "install.version",
                "'1.0.0-rc1' is not an exact version; byte 5 of a package version is not A to Z, a \
                 to z, 0 to 9 or .",
            ),
        },
        Deviation {
            surface: "server_file",
            vector: "long-version-plus",
            contract: "contract 01b §3.1: no grammar for a version",
            difference: Difference::Stricter(
                "install.version",
                "'1.0.0+local' is not an exact version; byte 5 of a package version is not A to Z, \
                 a to z, 0 to 9 or .",
            ),
        },
        Deviation {
            surface: "server_file",
            vector: "long-version",
            contract: "contract 01b §3.1: no grammar for a version",
            difference: Difference::Stricter(
                "install.version",
                "'11111111111111111111111111111111111111111111111111111111111111111' is not an \
                 exact version; a package version has 64 bytes or less",
            ),
        },
        Deviation {
            surface: "server_file",
            vector: "long-env-name",
            contract: "contract 01b §4.1: no grammar for the name of a variable",
            // `agent_family` has no limit. `ids::EnvName` takes the limit of
            // 64 bytes of the strictest Python copy.
            difference: Difference::Stricter(
                "run.env.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' is not an \
                 environment variable name; an environment variable name has 64 bytes or less",
            ),
        },
    ];

    fn deviation(surface: &str, vector: &str) -> Option<&'static Deviation> {
        DEVIATIONS
            .iter()
            .find(|row| row.surface == surface && row.vector == vector)
    }

    fn read_json(path: &str) -> Value {
        let text = fs::read_to_string(Path::new(DATA_DIR).join(path)).unwrap();
        let document: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(document["format"], FORMAT, "{path}");

        document
    }

    /// The vectors of one surface, and the context of its file.
    fn surface(name: &str) -> (Vec<Value>, Value) {
        let index = read_json("index.json");
        let row = index["surfaces"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["surface"] == name)
            .unwrap_or_else(|| panic!("the index holds no surface {name}"));
        let document = read_json(row["path"].as_str().unwrap());
        let vectors = document["vectors"].as_array().unwrap().clone();
        assert_eq!(document["surface"], name);
        assert_eq!(
            vectors.len(),
            usize::try_from(row["vectors"].as_u64().unwrap()).unwrap()
        );
        assert!(!vectors.is_empty(), "{name} holds no vector");

        (vectors, document["context"].clone())
    }

    fn base64_decode(text: &str) -> Vec<u8> {
        const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut out = Vec::new();
        let mut buffer = 0_u32;
        let mut bits = 0;
        for byte in text.bytes().filter(|byte| *byte != b'=') {
            let value = ALPHABET.iter().position(|one| *one == byte).unwrap();
            buffer = (buffer << 6) | u32::try_from(value).unwrap();
            bits += 6;
            if bits >= 8 {
                bits -= 8;
                out.push(u8::try_from((buffer >> bits) & 0xff).unwrap());
            }
        }

        out
    }

    /// The bytes of an input form: `text` or `base64`.
    fn bytes_of(form: &Value) -> Vec<u8> {
        if let Some(text) = form["text"].as_str() {
            return text.as_bytes().to_vec();
        }

        base64_decode(form["base64"].as_str().unwrap())
    }

    /// Each file of each registry of the repository that a vector names.
    fn registries() -> BTreeMap<String, Vec<(String, Vec<u8>)>> {
        let document = read_json(REGISTRIES_FILE);
        let mut found: BTreeMap<String, Vec<(String, Vec<u8>)>> = BTreeMap::new();
        for row in document["files"].as_array().unwrap() {
            found
                .entry(row["registry"].as_str().unwrap().to_owned())
                .or_default()
                .push((row["path"].as_str().unwrap().to_owned(), bytes_of(row)));
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
        registries: &BTreeMap<String, Vec<(String, Vec<u8>)>>,
        params: &Value,
        prefix: &str,
    ) -> Scratch {
        let scratch = Scratch::new();
        let registry = params["registry"].as_str().unwrap();
        for (path, bytes) in &registries[registry] {
            scratch.write(&format!("{prefix}{path}"), bytes);
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
        fn of(context: &Value) -> Option<Self> {
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
        /// The file has the Python shape, and a stricter id type refuses it.
        Parsed,
        /// The file has no value.
        Refused,
    }

    fn reach(surface: &str, vector: &str) -> Reach {
        match deviation(surface, vector).map(|row| &row.difference) {
            Some(Difference::Refused(_)) => Reach::Refused,
            Some(Difference::Stricter(..)) => Reach::Parsed,
            _ => Reach::Valid,
        }
    }

    fn error_json(loc: &str, msg: &str) -> Value {
        json!({"severity": "error", "loc": loc, "msg": msg, "downgraded": false})
    }

    /// `issues` with no issue `extra`. The row of `id` names no difference
    /// when the list does not hold that issue.
    fn without(issues: &mut Value, extra: &Value, id: &str, contract: &str) {
        let list = issues.as_array_mut().unwrap();
        let at = list.iter().position(|issue| issue == extra);
        let at =
            at.unwrap_or_else(|| panic!("{id}: the deviation row names no difference: {contract}"));
        list.remove(at);
    }

    /// Compares one report with one vector. For a vector with a deviation
    /// row, the report must hold the difference that the row names.
    fn check_report(surface: &str, vector: &Value, report: &Report) {
        let id = vector["id"].as_str().unwrap();
        let mut issues = serde_json::to_value(report.issues()).unwrap();
        match deviation(surface, id).map(|row| (&row.difference, row.contract)) {
            Some((Difference::Refused(msg), contract)) => {
                assert_eq!(vector["result"], "accepted", "{id}: {contract}");
                assert_eq!(
                    issues,
                    json!([error_json("<document>", msg)]),
                    "{id}: the deviation row names no difference"
                );

                return;
            }
            Some((Difference::OtherRefusal(msg), contract)) => {
                let wanted = json!([error_json("<document>", msg)]);
                assert_eq!(vector["result"], "refused", "{id}: {contract}");
                assert_ne!(
                    vector["issues"], wanted,
                    "{id}: the deviation row names no difference"
                );
                assert_eq!(issues, wanted, "{id}: {contract}");
                assert_eq!(report.state().as_str(), vector["status"], "{id}");

                return;
            }
            Some((Difference::Stricter(loc, msg), contract)) => {
                assert_eq!(vector["result"], "accepted", "{id}: {contract}");
                without(&mut issues, &error_json(loc, msg), id, contract);
                assert_eq!(issues, vector["issues"], "{surface} {id}");
                assert!(!report.ok(), "{id}: {contract}");

                return;
            }
            Some((Difference::OneMoreError(loc, msg), contract)) => {
                assert_eq!(vector["result"], "refused", "{id}: {contract}");
                without(&mut issues, &error_json(loc, msg), id, contract);
            }
            None => {}
        }

        assert_eq!(issues, vector["issues"], "{surface} {id}");
        assert_eq!(report.state().as_str(), vector["status"], "{surface} {id}");
        assert_eq!(
            if report.ok() { "accepted" } else { "refused" },
            vector["result"],
            "{surface} {id}"
        );
    }

    fn walk_family_file(surface_name: &str) {
        let (vectors, context) = surface(surface_name);
        let host = WrittenHost::of(&context);
        let host: Option<&dyn HostFacts> = host.as_ref().map(|host| -> &dyn HostFacts { host });
        let registries = registries();
        for vector in &vectors {
            let id = vector["id"].as_str().unwrap();
            let directory = vector["params"]["directory"].as_str().unwrap();
            let scratch = staged(&registries, &vector["params"], "");
            scratch.write(
                &format!("families/{directory}/family.yaml"),
                &bytes_of(&vector["input"]),
            );
            let loaded: Registry = load_registry(&scratch.0, host, &SystemZones::host());
            let report = &loaded.reports()[directory];
            check_report(surface_name, vector, report);
            let reach = reach(surface_name, id);
            if vector["result"] != "accepted" || reach == Reach::Refused {
                assert!(!loaded.families().contains_key(directory), "{id}");
                continue;
            }

            // The parsed file, and the valid family, hold each field as the
            // Python model holds it.
            let parsed = &loaded.parsed_families()[directory];
            assert_eq!(
                serde_json::to_value(parsed).unwrap(),
                vector["value"],
                "{id}"
            );
            if reach == Reach::Parsed {
                assert!(!loaded.families().contains_key(directory), "{id}");
                continue;
            }

            let family = &loaded.families()[directory];
            assert_eq!(
                serde_json::to_value(RawFamily::from(family)).unwrap(),
                vector["value"],
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
        let (vectors, _) = surface("server_file");
        let registries = registries();
        for vector in &vectors {
            let id = vector["id"].as_str().unwrap();
            let directory = vector["params"]["directory"].as_str().unwrap();
            let scratch = staged(&registries, &vector["params"], "");
            scratch.write(
                &format!("mcp/{directory}/server.yaml"),
                &bytes_of(&vector["input"]),
            );
            let loaded = load_registry(&scratch.0, None, &SystemZones::host());
            let report = &loaded.server_reports()[directory];
            check_report("server_file", vector, report);
            if vector["result"] != "accepted" {
                assert!(!loaded.servers().contains_key(directory), "{id}");
                continue;
            }

            let parsed = &loaded.parsed_servers()[directory];
            assert_eq!(
                serde_json::to_value(parsed).unwrap(),
                vector["value"],
                "{id}"
            );
            if reach("server_file", id) == Reach::Parsed {
                assert!(!loaded.servers().contains_key(directory), "{id}");
                continue;
            }

            let server = &loaded.servers()[directory];
            assert_eq!(
                serde_json::to_value(RawServer::from(server)).unwrap(),
                vector["value"],
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
        let (vectors, _) = surface("family_file.classify");
        for vector in &vectors {
            let id = vector["id"].as_str().unwrap();
            let parse = |key: &str| {
                let text = vector["input"]["args"][key].as_str().unwrap();
                let (raw, issues) = parse_family(text);

                raw.unwrap_or_else(|| panic!("{id} {key}: {issues:?}"))
            };
            let diff = classify(&parse("old"), &parse("new"));
            assert_eq!(vector["result"], "accepted", "{id}");
            assert_eq!(diff_value(&diff), vector["value"], "{id}");
        }
    }

    #[test]
    fn the_program_writes_the_python_output() {
        let (vectors, _) = surface("family_file.cli");
        let registries = registries();
        for vector in &vectors {
            let id = vector["id"].as_str().unwrap();
            let scratch = staged(&registries, &vector["params"], "registry/");
            let argv: Vec<&str> = vector["input"]["args"]["argv"]
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
            let wanted = if vector["result"] == "accepted" {
                &vector["value"]
            } else {
                &vector["refusal"]
            };
            assert_eq!(
                output.status.code().map(i64::from),
                wanted["exit"].as_i64(),
                "{id}"
            );
            assert_eq!(wanted["exit"] == 0, vector["result"] == "accepted", "{id}");
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
        let index = read_json("index.json");
        let own: Vec<&str> = index["surfaces"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["surface"].as_str().unwrap())
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
            let (vectors, _) = surface(row.surface);
            assert!(
                vectors.iter().any(|vector| vector["id"] == row.vector),
                "{} holds no vector {}",
                row.surface,
                row.vector
            );
            assert!(!row.contract.is_empty());
        }
    }
}
