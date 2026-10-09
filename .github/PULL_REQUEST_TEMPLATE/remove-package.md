This pull request removes the Python package `<package>`.

`CONTRIBUTING.md`, section "Removal of a Python package", holds each rule
and each numbered list that this text names. Replace each name in angle
brackets. Mark the box of a check or of a condition only when it holds.
Mark the box of a change only when the pull request makes it.

This text is public. Name no host and no family of a deployment. Describe
no defect. Write no evidence here.

## The package

- The directory of the package: `<directory>/`
- Each component that installed the package in its Python build:
  `<component>`

## Condition 1: each component is proven

Write one table row for each component of the list above. Keep the evidence
in a private place. In the last column, give only a link to the evidence,
for the owner. The link names no host, no user and no domain. A file name is
sufficient.

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

- [ ] Condition 2: the component catalog
- [ ] Condition 3: each other `pyproject.toml`

## The six changes

- [ ] Change 1: the source of the package, its tests and its `pyproject.toml`
- [ ] Change 2: the entries of the package in each config file
- [ ] Change 3: `uv.lock`
- [ ] Change 4: the vectors and the frozen files
- [ ] Change 5: the tests and the stand-ins
- [ ] Change 6: the rule files and the unit comments

Each stop case of change 4 and of change 5, with its decision. Write `none`
when the work had no stop case.

- `<surface or test>`: `<decision>`

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

`CONTRIBUTING.md` names who merges this pull request.
