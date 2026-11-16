//! The list of signing pairs of section 4.3 item 6.

use creche_ci_signer::pairs::{PairsError, SigningPairs};

fn owner() -> creche_ci_signer::names::Owner {
    "example-owner".parse().unwrap()
}

#[test]
fn builtin_pairs() {
    let pairs = SigningPairs::builtin(owner()).unwrap();
    let rows: Vec<(String, String)> = pairs
        .pairs()
        .map(|(repo, workflow)| (repo.to_string(), workflow.to_string()))
        .collect();

    // The table of section 4.3, sorted by name. registry has no row.
    assert_eq!(
        rows,
        [
            ("chaperone", "release.yml"),
            ("creche", "release.yml"),
            ("creche-release", "tags.yml"),
            ("mcp", "rework-component-tags.yml"),
        ]
        .map(|(repo, workflow)| (repo.to_owned(), workflow.to_owned()))
    );

    let creche = "example-owner/creche".parse().unwrap();
    assert_eq!(pairs.workflow_for(&creche).unwrap().as_str(), "release.yml");
    assert!(
        pairs
            .workflow_for(&"example-owner/registry".parse().unwrap())
            .is_none()
    );
    assert!(
        pairs
            .workflow_for(&"other-owner/creche".parse().unwrap())
            .is_none()
    );
    assert!(
        pairs
            .workflow_for(&"example-owner/Creche".parse().unwrap())
            .is_none()
    );
}

#[test]
fn refusals() {
    let parse = |text: &str| SigningPairs::parse(owner(), text).unwrap_err();

    assert_eq!(parse("creche\trelease.yml"), PairsError::NoFinalLf);
    assert_eq!(parse(""), PairsError::NoFinalLf);
    assert_eq!(parse("\n"), PairsError::BlankLine(1));
    assert_eq!(
        parse("creche\trelease.yml\n\nmcp\ttags.yml\n"),
        PairsError::BlankLine(2)
    );
    assert_eq!(
        parse("creche\trelease.yml\r\n"),
        PairsError::CarriageReturn(1)
    );
    assert_eq!(parse("creche release.yml\n"), PairsError::Fields(1));
    assert_eq!(parse("creche\trelease.yml\textra\n"), PairsError::Fields(1));
    assert!(matches!(
        parse("creche\t.github/release.yml\n"),
        PairsError::Name(1, _)
    ));
    assert!(matches!(
        parse("owner/creche\trelease.yml\n"),
        PairsError::Name(1, _)
    ));
    assert_eq!(
        parse("creche\trelease.yml\ncreche\ttags.yml\n"),
        PairsError::Duplicate(2)
    );
}
