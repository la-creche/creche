# env

This directory holds the package `creche-env`. The package is the one
reader of the env files of the product, and it holds the files of the
product. The root `AGENTS.md` applies here too.

## What the package holds

| File | What it holds |
|---|---|
| `src/creche_env/envfile.py` | The grammar, the layers, the trust check and the faults. |
| `src/creche_env/cli.py` | The command `creche-env`. Its docstring names each verb and each option. `creche_env_get` and `creche_env_check` of `bin/lib/envfile.sh` call it. |
| `src/creche_env/default.env` | The default of each key of the product. |
| `src/creche_env/example.env` | Each required key of the product, with an example value. |
| `src/creche_env/root-keys.txt` | Each key that a program of root trusts. |

The three data files are package data. A program finds them beside the
module, in a checkout and in an installed tree. A test of a built wheel
holds that the wheel has the three files.

## The files

1. The keys of `default.env` and of `example.env` are the catalog. An empty
   value of `default.env` makes an optional key with no default.
2. A comment line `#KEY=<example>` of `example.env` declares a required
   key, which has no default. A comment line `# text` is prose.
3. The host gives one `.env`: the site file, `/etc/creche/site.env`. The
   variable `AGENT_SITE_FILE` names another one in a test.
4. The `.env` overrides a default, key by key. It also sets each required
   key. A key outside the catalog is refused.
5. A secret never goes into one of the files. A value that a program writes
   for another program is state, and it stays out too. A bash script parses
   no file of the product: it calls the command.
6. A file outside the catalog is read in the grammar-only mode: the rules of
   a `.env`, with no catalog and no reference. `creche-env raw` reads one.

## The grammar

1. A file is UTF-8 with no byte order mark, and it has 64 KiB at most.
2. Each line ends with LF, and a file that is not empty ends with LF. A CR
   is refused.
3. A line is empty, a comment or an assignment. A comment starts with `#`
   in the first column.
4. After its `#`, a comment holds no backslash and no ASCII control
   character. A character outside ASCII is permitted.
5. An assignment is `KEY=VALUE`, with no space around `=`. A key matches
   `[A-Z][A-Z0-9_]{0,63}`.
6. A value holds printable ASCII and the space. It holds no quote, no
   backslash and no `#`. It does not start or end with a space.
7. A key is on one line of a file at most.
8. A value of a `.env` is not empty and holds no `$`. To take the default,
   remove the line.
9. A value of `default.env` can hold `${KEY}`, the value of another
   catalog key. It can also hold `${home:KEY}`, the home directory of the
   account that KEY names. It holds `$` in no other form.

## The layers

The reader applies these steps in this order:

1. Read the catalog. A key in the two files of the pair is refused.
2. Read the `.env`. A key outside the catalog is refused.
3. Check each required key. Each absent required key is a fault, and the
   reader names each one.
4. Replace each `${home:KEY}`. The password database gives the home.
5. Replace each `${KEY}`. A cycle is refused. A chain has 8 references at
   most.

The result holds each catalog key that has a value. A program gives the
result to its typed config, and the type checks each value.

## The faults

A fault names the file, the line and the key. It never names a value. Line
0 is a fault of the whole file. The command prints one line for each fault
and exits with status 78.

| Fault | Cause |
|---|---|
| `too-large` | A file has more than 64 KiB. A value has more after its references. |
| `not-utf8` | A file is not UTF-8. |
| `byte-order-mark` | A file starts with a byte order mark. |
| `carriage-return` | A line holds a CR. |
| `no-final-newline` | A file that is not empty does not end with LF. |
| `not-key-value` | A line has no `=`. A line of `example.env` is not a comment. A comment holds a backslash or an ASCII control character. |
| `bad-key` | A key is not in the grammar. |
| `bad-value` | A value is not in the grammar. A home is not a value of a `.env`. |
| `empty-value` | A value is empty where the file needs a value. |
| `duplicate-key` | A key is on two lines, or in the two files of the pair. |
| `unknown-key` | The `.env` sets a key outside the catalog. |
| `reference-in-env` | A value outside `default.env` holds `$`. |
| `unknown-reference` | A reference names a key outside the catalog. |
| `unset-reference` | A reference names a key with no value. |
| `reference-cycle` | A chain of references comes back to a key of the chain. |
| `reference-too-deep` | A chain has more than 8 references. |
| `unknown-account` | The password database has no account of that name. |
| `required-unset` | The `.env` does not set a required key. |
| `environment-differs` | A catalog key of the environment has another value than the files. |
| `wrong-owner` | The owner of the `.env` is not root and not the reader. |
| `others-write` | Group or other can write the `.env`. |

A file that the process cannot open or read is no fault of a text. The
command prints `<file>: cannot be read: <reason>` and exits with status 78.

## Rules for a change

1. Do not change a value of `default.env`. For another value, add a key.
   Remove the old key in a later change, and never add it again. Reason: a
   release installs one copy of the file in each tree, so two trees can
   hold two versions. Each key of both versions must then have one value.
   `bin/tests/test_envfile_catalog.py` compares each key with each earlier
   version of the file on the base branch.
2. A key gets a default when each host can use one value. A key with no
   such value is a required key of `example.env`.
3. A default names no host, no user, no domain and no address of a
   deployment. An example value uses `example-owner`, `operator` and
   `192.0.2.10`. The catalog test refuses an IPv4 address outside
   192.0.2.0/24 and 127.0.0.0/8.
4. A key that names a path below a root takes a reference to that root.
   One line of the `.env` then moves each path below the root.
5. A key that a program of root reads is a line of `root-keys.txt`.
6. The package uses the standard library only.
7. A change of the grammar or of a layer changes the vectors of
   `envfile.*` in the same pull request. The Rust reader must give the same
   result for each vector.
8. A component that installs `creche-env` bundles `env`. A change here then
   moves the tag of that component. No component installs the package yet,
   so a change here moves no tag. A change of `uv.lock` moves the tag of
   each venv component.

## Known gaps

- No program of a component reads the package yet. Each program keeps its
  reader of today until a change moves it to this package.
- No program applies the rule of the environment yet. `check_environment`
  holds the rule for a program of root. The command reads no catalog key
  from its environment.
- The path of the site file has two sources: `envfile.SITE_FILE` and
  `handover.site.SITE_FILE`. The second one goes when `handover` reads the
  site file through this package.
- CONTRACT-QUESTION: no contract text gives these readings, and each one
  is the most strict. A change costs the reader, its vectors and the Rust
  reader.
  1. The check of the required keys comes before layers 4 and 5.
  2. The grammar-only mode takes the rules of a `.env` value.
  3. A value has 64 KiB at most after its references.
  4. A line `#text` of `example.env` with no space must declare a key.
  5. A home of the password database must be a value of a `.env`.
  6. A file that the process cannot read has no name in the fault set.
  7. `changed-trusted` compares the values after each layer.
  8. A comment holds no backslash and no ASCII control character.
