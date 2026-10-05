This pull request removes the Python package `<package>`.

`CONTRIBUTING.md`, section "Removal of a Python package", holds each rule
and each numbered list that this text names. Replace each name in angle
brackets. Mark a box only when its line is true.

This text is public. Name no host and no family of a deployment. Describe
no defect. Write no evidence here.

## The package

- Directory of the package: `<directory>/`
- Each component whose Python build installed the package: `<component>`

## Condition 1: each component is proven

Write one line for each component of the list above. Keep the evidence in a
private place. In the last column, give only a link to the evidence, for the
owner.

| Component | Cutover tag | Evidence, for the owner |
|---|---|---|
| `<component>` | `<cutover tag>` | `<link>` |

Each of the six checks holds for each component of the table:

- [ ] Check 1: CI
- [ ] Check 2: the host
- [ ] Check 3: time
- [ ] Check 4: no fault
- [ ] Check 5: use
- [ ] Check 6: defects

## Conditions 2 and 3

- [ ] Condition 2: no row of the component catalog names the directory.
- [ ] Condition 3: no `pyproject.toml` of another package names the package.

## The six changes

- [ ] Change 1: the source, the tests and the `pyproject.toml` of the package
- [ ] Change 2: the entries of the package in each config file
- [ ] Change 3: `uv.lock`
- [ ] Change 4: the vectors, with a SHA-256 pin for each kept file
- [ ] Change 5: the tests and the stand-ins
- [ ] Change 6: the rule files and the unit comments

## Tags and releases

The output of the tag allocator with `--dry-run`, on the head of the branch:

```text
<output>
```

Each release that the merge starts, one line for each component. Write
`none` when the merge starts no release.

- `<component>`

## The way back

Revert this pull request.

## Merge

The author does not merge this pull request. `CONTRIBUTING.md` names who
merges it.
