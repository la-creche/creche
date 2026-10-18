//! The differential test of `creche_contracts::config::envfile`.
//!
//! The test walks each vector of the three surfaces `envfile.*`, through
//! the public items of the module only. The Rust result must equal the
//! result of the Python reader for each vector: the settings or the faults,
//! with each fault, file, line and key in the order of the Python reader.

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use creche_contracts::config::Env;
    use creche_contracts::config::envfile::{
        EnvError, Problem, Text, Uid, check_environment, check_trust, load, raw, read_pair,
    };
    use creche_vectors::{Input, Outcome, Vector};
    use serde_json::{Value, json};

    /// The name that each fault of the trust surface gives the file.
    const TRUSTED_NAME: &str = "site.env";

    /// The name that each fault of the raw surface gives the file.
    const PLAIN_NAME: &str = "plain.env";

    /// What the reader did with one input: the value, or the faults as a
    /// vector holds them. The type is not `Result`: the lint gate refuses
    /// an `unwrap` in a function that returns `Result`.
    #[derive(Debug, PartialEq)]
    enum Did {
        Took(Value),
        Refused(Value),
    }

    /// One surface and the function that replays a vector of it.
    type Replay = (&'static str, fn(&Vector) -> Did);

    /// Each surface of the module. A test below fails when the index holds
    /// an `envfile.` surface that this table does not name.
    const SURFACES: &[Replay] = &[
        ("envfile.layer", layer),
        ("envfile.raw", plain),
        ("envfile.trust", trust),
    ];

    /// The result of one call, with each fault in the form of a vector.
    fn did(result: Result<Value, EnvError>) -> Did {
        let problem = |one: &Problem| {
            json!({
                "fault": one.fault().word(),
                "file": one.file(),
                "line": one.line(),
                "key": one.key().map_or("", |key| key.as_str()),
            })
        };

        match result {
            Ok(value) => Did::Took(value),
            Err(error) => Did::Refused(error.problems().iter().map(problem).collect()),
        }
    }

    /// The file of the named argument `key`.
    fn file(input: &Input, key: &str) -> Text {
        let file = input.file(key).unwrap();

        Text::new(file.name(), file.bytes())
    }

    /// The replay of `envfile.layer`: the pair, the layers with `homes` as
    /// the password database, then the check of `environ`.
    fn layer(vector: &Vector) -> Did {
        let input = vector.input();
        let arguments = input.args().unwrap();
        let homes: BTreeMap<String, String> =
            serde_json::from_value(arguments["homes"].clone()).unwrap();
        let environ: Vec<(String, String)> =
            serde_json::from_value(arguments["environ"].clone()).unwrap();
        let env = file(input, "env");

        did(
            read_pair(&file(input, "default"), &file(input, "example")).and_then(|pair| {
                let settings = load(&pair, &env, |account| homes.get(account).cloned())?;
                check_environment(&settings, &Env::from_pairs(environ))?;
                let values: BTreeMap<&str, &str> = settings
                    .values()
                    .map(|(key, value)| (key.as_str(), value))
                    .collect();

                Ok(json!(values))
            }),
        )
    }

    /// The replay of `envfile.raw`: one file in the grammar-only mode.
    fn plain(vector: &Vector) -> Did {
        let bytes = vector.input().bytes().unwrap();

        did(raw(&Text::new(PLAIN_NAME, bytes)).map(|lines| {
            let values: BTreeMap<&str, &str> = lines
                .iter()
                .map(|line| (line.key().as_str(), line.value()))
                .collect();

            json!(values)
        }))
    }

    /// The replay of `envfile.trust`: the owner, the mode and the reader.
    fn trust(vector: &Vector) -> Did {
        let arguments = vector.input().args().unwrap();
        let number = |key: &str| u32::try_from(arguments[key].as_u64().unwrap()).unwrap();
        let (owner, reader) = (Uid::new(number("owner")), Uid::new(number("reader")));

        did(check_trust(TRUSTED_NAME, owner, number("mode"), reader).map(|()| Value::Null))
    }

    #[test]
    fn the_reader_gives_the_python_result_for_each_vector() {
        let mut differ = Vec::new();
        for (name, replay) in SURFACES {
            for vector in creche_vectors::surface(name).unwrap().vectors() {
                let got = replay(vector);
                let equal = match (vector.result(), &got) {
                    (Outcome::Accepted, Did::Took(value)) => {
                        vector.value().unwrap_or(&Value::Null) == value
                    }
                    (Outcome::Refused, Did::Refused(faults)) => vector.refusal() == Some(faults),
                    (Outcome::Raised, Did::Refused(_)) => true,
                    (Outcome::Accepted | Outcome::Refused | Outcome::Raised, _) => false,
                };
                if !equal {
                    differ.push(format!("{name} {}: {got:?}", vector.id()));
                }
            }
        }

        assert_eq!(differ, Vec::<String>::new());
    }

    #[test]
    fn each_surface_of_the_reader_is_in_the_table() {
        let unnamed: Vec<String> = creche_vectors::index()
            .unwrap()
            .iter()
            .map(|row| row.surface().to_owned())
            .filter(|surface| surface.starts_with("envfile."))
            .filter(|surface| SURFACES.iter().all(|(name, _)| name != surface))
            .collect();

        assert_eq!(unnamed, Vec::<String>::new());
    }
}
