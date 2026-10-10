#!/usr/bin/env bash
# OPERATOR and CI: the Rust half of the quality gate, for the workspace under
# rust/. bin/quality-gate.sh runs it for a change that touches rust/
# (bin/lib/rustrule.sh). The `rust` job of gate.yml and release.yml runs it.
#   bin/rust-gate.sh           the [lints] check, the include check, the
#                              panic check, the public-field check, cargo
#                              fmt, cargo clippy and cargo deny
#   bin/rust-gate.sh --tests   the same, then cargo test
#   bin/rust-gate.sh --advisories
#                              the advisory check of cargo deny, and no other
#                              step. The gate does not make that check.
#                              .github/workflows/advisories-daily.yml runs
#                              this mode one time a day
# Needs cargo on PATH. rustup takes the toolchain from
# rust/rust-toolchain.toml, so every cargo step runs inside rust/.
# `cargo deny` needs cargo-deny on PATH. CI installs it. A developer machine
# without it prints one line and runs the other steps. `--advisories` fails
# without it on each machine.
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/../rust"

#: The flag that adds the tests.
WITH_TESTS="--tests"

#: The flag that runs the advisory check of `cargo deny` and no other step.
ADVISORIES_ONLY="--advisories"

#: The file of the workspace itself. Every other Cargo.toml is a crate.
WORKSPACE_MANIFEST="./Cargo.toml"

#: cargo's own build directory. A directory named target anywhere else is
#: source: crates/target is a workspace member like any other.
BUILD_DIR="./target"

#: A line of Rust that includes a Markdown file, e.g.
#: `#![doc = include_str!("../README.md")]`.
MD_INCLUDE='include(_str|_bytes)?!.*\.md"'

#: The directory of the crates. Each directory in it is one crate.
CRATES_DIR="./crates"

#: The directory of a crate that holds its test targets. Each file below
#: crates/<name>/tests is test code. A directory with this name at another
#: depth does not count: with `pub mod tests;`, crates/<name>/src/tests is
#: code of the crate. crates/tests is a crate like any other.
TESTS_DIR="tests"

#: The line that marks a test module. It stands directly above the first
#: line of the module.
TEST_MARK='#[cfg(test)]'

#: awk rules that leave out the test code of a source file. Each text scan
#: below starts with them, and gives them TEST_MARK as `mark`.
#:
#: Test code is each module with a body that has a TEST_MARK line directly
#: above it, from its first line to the line that closes it. That line
#: starts with `}` at the indent of the first line, and only a comment can
#: follow the `}`: `cargo fmt` writes a module in that form. A scan reads the
#: code after that line again.
#:
#: A TEST_MARK line above another item starts no test code, e.g. above
#: `mod python;`. A scan reads that item and the code after it.
#:
#: A test module with no such last line hides the code below it. The rules
#: keep the file of each one in `open_modules`, with the count in `opened`.
#: The END rule of a scan reads the two, and its check fails for such a file.
TEST_CODE_AWK='
  function trimmed(text) {
    gsub(/^[ \t]+|[ \t]+$/, "", text)
    return text
  }

  function note_open() {
    if (in_tests) open_modules[++opened] = module_file
    in_tests = 0
  }

  # A line that ends with CR LF reads as a line that ends with LF. Each other
  # CR is white space, as it is for the compiler.
  { sub(/\r$/, ""); gsub(/\r/, " ") }

  FNR == 1 { note_open(); marked = 0 }

  in_tests {
    if (index($0, module_end) == 1) {
      after_end = trimmed(substr($0, length(module_end) + 1))
      comment = substr(after_end, 1, 2)
      if (after_end == "" || comment == "//" || comment == "/*") in_tests = 0
    }

    next
  }

  marked && /^[ \t]*(pub(\((crate|super)\))? +)?mod +[A-Za-z0-9_]+ *[{]$/ {
    module_end = $0
    sub(/[^ \t].*$/, "}", module_end)
    module_file = FILENAME
    in_tests = 1
    marked = 0
    next
  }

  { marked = (trimmed($0) == mark) }

  END { note_open() }
'

#: The exit status of a scan for a file that ends inside a test module. awk
#: itself exits with 2 for an error of its own.
NO_MODULE_END=3

#: A character that is no part of a Rust name. A word has one on each side,
#: or the start or the end of its line.
NAME_EDGE='[^A-Za-z0-9_]'

#: The call that catches a panic, and the pattern of that word in a line.
CATCH_NAME="catch_unwind"
CATCH_WORD="(^|$NAME_EDGE)$CATCH_NAME(\$|$NAME_EDGE)"

#: The crate that holds each panic boundary of a service, and a crate file
#: that names it: the name with no letter, no digit, no `_` and no `-` beside
#: it. Each line of the file counts, a comment too.
RUNTIME_CRATE="creche-runtime"
RUNTIME_NAMED="(^|[^A-Za-z0-9_-])$RUNTIME_CRATE(\$|[^A-Za-z0-9_-])"

#: The file of a crate that holds the entry function of a program with no
#: runtime, below the directory of the crate.
ENTRY_FILE="src/entry.rs"

#: The crates that the public-field check does not read yet, with one space
#: between two names. Each one still has a struct with a public field. The
#: change that makes the fields of a crate private deletes its name here.
FIELD_CHECK_SKIPS="agent-family creche-contracts creche-runtime creche-testkit"

#: The scan of the public-field check, for awk, after TEST_CODE_AWK. It
#: prints one line for each field of a struct that has `pub` with no
#: `(crate)` and no `(super)` after it. It needs four more variables:
#: `root` is CRATES_DIR, `tests` is TESTS_DIR, `skipped` is
#: FIELD_CHECK_SKIPS, and `quote` is the single quote, which a text in
#: single quotes cannot hold.
#:
#: A struct item starts at a line whose first word, after a visibility, is
#: `struct`. `feed` takes the text of the item to its end: the `;`, or the
#: `}` of its field block. It drops each comment, each string and each
#: character literal, so a bracket or the word `pub` in one of them counts
#: for nothing. `finish` then splits the field list at each comma outside a
#: bracket, and `check` reads the visibility of one field.
#:
#: The scan reads no field of an enum variant, which can have no `pub`, and
#: no `pub` of another item. It reads the text and expands no macro. When it
#: finds no end of a struct or of a test module, it prints a line for that
#: file: the check then fails, and no field passes with no read.
FIELD_SCAN_AWK='
  BEGIN {
    # The start of a struct item: a visibility or none, then the word and
    # the white space after it. The name of the struct comes next.
    item_start = "^(pub[ \t]*([(][^)]*[)])?[ \t]+)?struct[ \t]+"

    # The name of a struct: each character up to the first one that can
    # follow a name. A name can hold a letter that is not ASCII.
    item_name = "[^ \t<({;]+"
  }

  function not_read(path,    crate) {
    crate = substr(path, length(root) + 2)
    sub(/\/.*$/, "", crate)

    if (index(" " skipped " ", " " crate " ") > 0) return 1

    return index(path, root "/" crate "/" tests "/") == 1
  }

  function unread() {
    if (reading) {
      print file " holds the struct `" name "`, and the public-field check found no end of it"
    }

    reading = 0
  }

  function feed(text,    i, n, c, two, tail, last) {
    n = length(text)

    for (i = 1; i <= n; i++) {
      c = substr(text, i, 1)
      two = substr(text, i, 2)

      if (mode == "comment") {
        if (two == "/*") { nest++; i++ }
        else if (two == "*/") { nest--; i++ }

        if (nest == 0) mode = "code"
        continue
      }

      if (mode == "string") {
        if (c == "\\") i++
        else if (c == "\"") mode = "code"
        continue
      }

      if (mode == "raw") {
        if (c == "\"" && substr(text, i + 1, length(hashes)) == hashes) {
          i += length(hashes)
          mode = "code"
        }
        continue
      }

      if (two == "//") break

      if (two == "/*") { mode = "comment"; nest = 1; i++; continue }

      if (c == "\"") {
        tail = item
        hashes = ""

        while (substr(tail, length(tail)) == "#") {
          hashes = hashes "#"
          tail = substr(tail, 1, length(tail) - 1)
        }

        mode = ((" " tail) ~ /[^A-Za-z0-9_]b?r$/) ? "raw" : "string"
        continue
      }

      if (c == quote) {
        if (substr(text, i + 1, 1) == "\\") {
          last = index(substr(text, i + 3), quote)
          if (last > 0) { i += 2 + last; continue }
        } else if (substr(text, i + 2, 1) == quote) {
          i += 2
          continue
        }
      }

      item = item c

      if (c == "(" || c == "[" || c == "{") depth++
      else if (c == ")" || c == "]" || c == "}") depth--

      if (depth == 0 && (c == "}" || c == ";")) { finish(); return }
    }

    item = item " "
  }

  function group_end(text,    i, n, c, round, angle) {
    n = length(text)

    for (i = 1; i <= n; i++) {
      c = substr(text, i, 1)

      if (c == "(" || c == "[" || c == "{") round++
      else if (c == ")" || c == "]" || c == "}") round--
      else if (c == "<" && round == 0) angle++
      else if (c == ">" && round == 0) angle--
      else continue

      if (round == 0 && angle == 0) return i
    }

    return 0
  }

  # The place of the first `{` outside `(` and `[`: the field block. A brace
  # inside one of the two is a part of a type, e.g. of `[u8; { N }]`. The
  # result is 0 for no such brace, and -1 for a brace inside `<` and `>`,
  # which is a part of a type too.
  function block_start(text,    i, n, c, round, angle) {
    n = length(text)

    for (i = 1; i <= n; i++) {
      c = substr(text, i, 1)

      if (c == "{" && round == 0) return (angle == 0) ? i : -1

      if (c == "(" || c == "[" || c == "{") round++
      else if (c == ")" || c == "]" || c == "}") round--
      else if (c == "<" && round == 0) angle++
      else if (c == ">" && round == 0) angle--
    }

    return 0
  }

  function check(piece, kind, place,    text, last, inner, field) {
    text = piece

    while (1) {
      sub(/^[ \t]+/, "", text)
      if (substr(text, 1, 1) != "#") break

      last = group_end(text)
      if (last == 0) return 0

      text = substr(text, last + 1)
    }

    if (text == "") return 0
    if ((text " ") !~ /^pub[^A-Za-z0-9_]/) return 1

    text = substr(text, 4)
    sub(/^[ \t]+/, "", text)

    if (substr(text, 1, 1) == "(") {
      last = index(text, ")")
      inner = substr(text, 2, last - 2)
      gsub(/[ \t]/, "", inner)

      # CONTRACT-QUESTION: rule 12 of rust/AGENTS.md names two forms that
      # are not public, `pub(crate)` and `pub(super)`. It does not name
      # `pub(self)` and `pub(in <path>)`. Reading taken: only the two named
      # forms pass. A change costs this one condition.
      if (inner == "crate" || inner == "super") return 1
      if (kind == "named") text = substr(text, last + 1)
    }

    field = place

    if (kind == "named") {
      field = text
      sub(/:.*$/, "", field)
      field = trimmed(field)
    }

    print file " gives the struct `" name "` the public field `" field "`"
    return 1
  }

  function finish(    text, kind, start, last, body, i, n, c, round, angle, piece, place) {
    text = item
    gsub(/->/, "  ", text)
    sub(item_start item_name "[ \t]*", "", text)

    if (substr(text, 1, 1) == "<") {
      last = group_end(text)
      if (last == 0) { unread(); return }

      text = substr(text, last + 1)
      sub(/^[ \t]+/, "", text)
    }

    kind = (substr(text, 1, 1) == "(") ? "tuple" : "named"
    start = (kind == "tuple") ? 1 : block_start(text)
    if (start == 0) { reading = 0; return }

    # A brace inside `<` and `>` is no field block: `feed` stopped too early.
    if (start < 0) { unread(); return }

    text = substr(text, start)
    last = group_end(text)
    if (last == 0) { unread(); return }

    reading = 0
    body = substr(text, 2, last - 2) ","
    n = length(body)
    place = 0

    for (i = 1; i <= n; i++) {
      c = substr(body, i, 1)

      if (c == "," && round == 0 && angle == 0) {
        place += check(piece, kind, place)
        piece = ""
        continue
      }

      if (c == "(" || c == "[" || c == "{") round++
      else if (c == ")" || c == "]" || c == "}") round--
      else if (c == "<" && round == 0) angle++
      else if (c == ">" && round == 0) angle--

      piece = piece c
    }
  }

  FNR == 1 {
    unread()
    skip = not_read(FILENAME)
  }

  skip { next }

  !reading {
    head = $0
    sub(/^[ \t]+/, "", head)
    if (head !~ (item_start item_name)) next

    name = head
    sub(item_start, "", name)
    match(name, "^" item_name)
    name = substr(name, 1, RLENGTH)
    file = FILENAME
    reading = 1
    item = ""
    depth = 0
    mode = "code"
    feed(head)
    next
  }

  { feed($0) }

  END {
    unread()

    for (i = 1; i <= opened; i++) {
      if (not_read(open_modules[i])) continue

      print open_modules[i] " holds a test module, and the public-field check found no end of it"
    }
  }
'

#: The program behind `cargo deny`. cargo finds a subcommand on PATH by this
#: name.
DENY_PROGRAM="cargo-deny"

#: The checks of `cargo deny` that the gate makes for each change. Each one
#: reads the locked crates and rust/deny.toml, and no data of another place.
#: Its result thus changes only when the tree changes.
DENY_GATE_CHECKS=(bans licenses sources)

#: The check of `cargo deny` that the gate does not make. It reads the
#: advisory database from the network, so its result can change while the
#: tree stays the same. ADVISORIES_ONLY makes this check. The two lists
#: together name each check of rust/deny.toml: bin/tests/test_rust_gate.py
#: holds that.
DENY_SCHEDULED_CHECKS=(advisories)

# inherits_lints MANIFEST: whether the crate takes the lint gate of
# rust/Cargo.toml. Only one spelling passes: a `[lints]` table that holds the
# line `workspace = true`.
inherits_lints() {
  awk '
    { sub(/^[ \t]+/, "") }
    /^\[/ { in_lints = ($0 ~ /^\[lints\][ \t]*(#.*)?$/); next }
    in_lints && /^workspace[ \t]*=[ \t]*true[ \t]*(#.*)?$/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$1"
}

# lints_inherited: fails when a crate does not take the lint gate, with one
# line per crate. A crate with no [lints] table builds with no lint of
# [workspace.lints] at all, and cargo does not say so. A crate is every
# Cargo.toml under rust/ but the workspace file and cargo's own target/. A
# search that finds no crate has checked nothing, and fails too.
lints_inherited() {
  local manifest crates=0 missing=0

  while IFS= read -r manifest; do
    crates=$((crates + 1))

    if inherits_lints "$manifest"; then
      continue
    fi

    echo "rust-gate: rust/${manifest#./} has no \`[lints] workspace = true\`" >&2
    missing=$((missing + 1))
  done < <(find . -name Cargo.toml ! -path "$WORKSPACE_MANIFEST" ! -path "$BUILD_DIR/*" |
    LC_ALL=C sort)

  if [[ "$crates" -eq 0 ]]; then
    echo "rust-gate: the \`[lints]\` check read no crate under rust/" >&2
    return 1
  fi

  [[ "$missing" -eq 0 ]]
}

# no_md_included: fails when a Rust source file includes a Markdown file,
# with one line per file. A change of nothing but Markdown runs no cargo step
# (bin/lib/docsrule.sh), so a doc test that reads a .md could break with no
# cargo run. The check reads the text line by line: an include macro and a
# name that ends in .md on one line.
no_md_included() {
  local source found=0

  while IFS= read -r source; do
    echo "rust-gate: rust/${source#./} includes a Markdown file" >&2
    found=$((found + 1))
  done < <(find . -name '*.rs' ! -path "$BUILD_DIR/*" -exec grep -lE "$MD_INCLUDE" {} + |
    LC_ALL=C sort)

  [[ "$found" -eq 0 ]]
}

# names_runtime MANIFEST: whether the crate file names the runtime crate. A
# file that grep cannot read counts as a file that does.
names_runtime() {
  local status=0

  grep -qE "$RUNTIME_NAMED" "$1" 2>/dev/null || status=$?

  [[ "$status" -ne 1 ]]
}

# catch_permitted SOURCE: whether SOURCE is one of the three places that can
# hold CATCH_NAME (rust/AGENTS.md, "The panic rule", clause 8):
#   1. a file of the runtime crate
#   2. the entry file of a crate whose crate file does not name the runtime
#      crate
#   3. test code: a file below the directory TESTS_DIR of its crate, or a
#      file that holds the word only in its test code
# The status is NO_MODULE_END for a file that ends inside a test module: the
# scan then read no code below the first line of that module.
catch_permitted() {
  local crate

  crate="${1#"$CRATES_DIR"/}"
  crate="${crate%%/*}"

  case "$1" in
    "$CRATES_DIR/$RUNTIME_CRATE"/* | "$CRATES_DIR/$crate/$TESTS_DIR"/*)
      return 0
      ;;
  esac

  if [[ "$1" == "$CRATES_DIR/$crate/$ENTRY_FILE" ]] &&
    ! names_runtime "$CRATES_DIR/$crate/Cargo.toml"; then
    return 0
  fi

  LC_ALL=C awk -v mark="$TEST_MARK" -v word="$NAME_EDGE$CATCH_NAME$NAME_EDGE" \
    -v no_end="$NO_MODULE_END" "$TEST_CODE_AWK"'
    (" " $0 " ") ~ word { stray = 1; exit }
    END { exit stray ? 1 : (opened ? no_end : 0) }
  ' "$1"
}

# no_stray_catch: fails when a Rust source file under rust/crates holds
# CATCH_NAME outside the three places of catch_permitted, with one line per
# file. A reviewer then knows where each panic boundary is. The check reads
# the text, so the word in a comment counts too. It also fails, with a line
# of its own, for a file with the word that ends inside a test module.
no_stray_catch() {
  local source status found=0

  while IFS= read -r source; do
    status=0
    catch_permitted "$source" || status=$?

    case "$status" in
      0)
        continue
        ;;
      "$NO_MODULE_END")
        echo "rust-gate: rust/${source#./} holds a test module, and the panic check found no end of it" >&2
        ;;
      *)
        echo "rust-gate: rust/${source#./} holds \`$CATCH_NAME\` outside the three places of the panic rule" >&2
        ;;
    esac

    found=$((found + 1))
  done < <(find "$CRATES_DIR" -name '*.rs' -exec grep -lE "$CATCH_WORD" {} + |
    LC_ALL=C sort)

  [[ "$found" -eq 0 ]]
}

# no_public_field: fails when a struct has a public field, with one line per
# field (rust/AGENTS.md, rule 12). Other code can write such a field, so no
# constructor checks its value. Only the forms `pub(crate)` and `pub(super)`
# pass. The check reads each crate under rust/crates that FIELD_CHECK_SKIPS
# does not name, and it reads no test code. A scan that stops with an error
# fails the check. So does a file that ends inside a test module.
no_public_field() {
  local fields line found=0

  if ! fields="$(LC_ALL=C find "$CRATES_DIR" -name '*.rs' -exec \
    awk -v mark="$TEST_MARK" -v root="$CRATES_DIR" -v tests="$TESTS_DIR" \
    -v skipped="$FIELD_CHECK_SKIPS" -v quote="'" "$TEST_CODE_AWK$FIELD_SCAN_AWK" {} + |
    LC_ALL=C sort)"; then
    echo "rust-gate: the public-field check did not read rust/${CRATES_DIR#./}" >&2
    return 1
  fi

  while IFS= read -r line; do
    if [[ -z "$line" ]]; then
      continue
    fi

    echo "rust-gate: rust/${line#./}" >&2
    found=$((found + 1))
  done <<< "$fields"

  [[ "$found" -eq 0 ]]
}

# deny_checked: the supply-chain check of the locked crates, against
# rust/deny.toml: the bans, the licenses and the sources (DENY_GATE_CHECKS).
# `--locked` refuses a Cargo.lock that the manifests no longer match. The
# advisories are not in this check: a new advisory would fail a change that
# touches no dependency. advisories_checked reads them.
#
# The check needs cargo-deny, which rustup does not install. Without it on
# PATH, a developer machine prints one line and passes: CI runs the check for
# the same change. In CI a missing cargo-deny fails. A runner sets CI, and a
# `rust` job that lost its install step must not pass with no check.
deny_checked() {
  if command -v "$DENY_PROGRAM" >/dev/null; then
    cargo deny --locked check "${DENY_GATE_CHECKS[@]}"
    return
  fi

  if [[ -n "${CI:-}" ]]; then
    echo "rust-gate: $DENY_PROGRAM not on PATH: CI must run the \`cargo deny\` check" >&2
    return 1
  fi

  echo "rust-gate: $DENY_PROGRAM not on PATH: no \`cargo deny\` check. CI runs the check"
}

# advisories_checked: the advisory check of the locked crates, against
# rust/deny.toml (DENY_SCHEDULED_CHECKS). It fails for a locked crate with an
# advisory that the policy refuses, and for a locked version that its author
# removed from crates.io. It reads the advisory database from the network.
#
# The check is the whole of its mode, so a machine without cargo or without
# cargo-deny fails: a run that passed there would be a run that read no
# advisory.
advisories_checked() {
  local program

  for program in cargo "$DENY_PROGRAM"; do
    if ! command -v "$program" >/dev/null; then
      echo "rust-gate: $program not on PATH: $ADVISORIES_ONLY needs it" >&2
      return 1
    fi
  done

  cargo deny --locked check "${DENY_SCHEDULED_CHECKS[@]}"
}

MODE="${1:-}"
case "$MODE" in
  "" | "$WITH_TESTS" | "$ADVISORIES_ONLY") ;;
  *)
    echo "usage: bin/rust-gate.sh [$WITH_TESTS | $ADVISORIES_ONLY]" >&2
    exit 2
    ;;
esac

if [[ "$MODE" == "$ADVISORIES_ONLY" ]]; then
  advisories_checked
  echo "rust-gate: PASS, for the advisory check only"
  exit 0
fi

command -v cargo >/dev/null || {
  echo "rust-gate: cargo not on PATH: a change under rust/ needs the Rust toolchain" >&2
  exit 1
}

lints_inherited
no_md_included
no_stray_catch
no_public_field
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
deny_checked

if [[ "$MODE" == "$WITH_TESTS" ]]; then
  cargo test --workspace --locked
fi

echo "rust-gate: PASS"
