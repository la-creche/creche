# creche-util

The shared helpers of this workspace. A shared helper is a pure function
with two users. `rust/AGENTS.md` and the root `AGENTS.md` apply here too.

The crate has no dependency, and each other crate of the workspace can use
it. A change to a function here changes the result in each crate that calls
the function.

## Layout

| Path | What it holds |
|---|---|
| `src/sha256.rs` | `digest`: the SHA-256 of FIPS 180-4, as 32 bytes. |
| `src/hex.rs` | `lower`: bytes as hex text in lower case. The text is the base 16 form of RFC 4648 section 8. |
| `src/pytext.rs` | `is_space`, `strip` and `words`: the white space rules of `str.isspace`, `str.strip` and `str.split` of Python. |

## What the crate takes

A function enters this crate only when each rule below is true for it. Each
rule has its reason.

1. **It is a pure function.** Its arguments are bytes or text, and its result
   depends on them only. It opens no file. It reads no clock and no variable
   of the environment. It is a plain `fn`, not an `async fn`.
   Reason: each crate can then call it from each place. A test needs only an
   input and its answer.
2. **It is free of contract rules.** It checks no id grammar. It names no
   kind of file of the platform. It cites no section of a contract.
   Reason: a rule of a contract has one home, in `creche-contracts`. A helper
   with such a rule is a second home for it.
3. **It has two users.** The two users are two crates, or two modules that
   hold two contracts. A function with one user stays in the module of that
   user. The functions of one published rule count as one helper: `is_space`,
   `strip` and `words` are the white space rule of Python `str`.
   Reason: a reader finds a function with one user beside that user. This
   crate must not collect each small function of the workspace.
4. **The standard library is enough for it.** The crate file has no
   dependency table. `bin/tests/test_rust_workspace.py` fails when the crate
   file names a dependency.
   Reason: each crate of the workspace can depend on this one. A dependency
   here then becomes a dependency of each of them.
5. **It follows a published rule.** A published rule is a standard, an RFC
   or the behavior of a function of Python. The doc comment of the helper
   names the rule. A test compares the helper with known answers of the
   rule.
   Reason: each caller trusts the result. The revision of a registry and the
   id of an approval gate each come from a digest.
6. **It is safe to publish.** It names no deployment. It holds no secret and
   no value of a host.
   Reason: this repository is public. Each workspace that takes a crate from
   this repository can get this crate with it.

## How to move a helper here

1. Find each copy of the helper in the workspace.
2. Move the copy that has the most tests, with those tests. Change its body
   only where the new place needs a change.
3. Keep each known answer of each copy in the tests here.
4. Delete each other copy in the same pull request. Make each user call the
   function here.
5. Add the row of the new file to "Layout".

A caller can add a prefix to a result, or cut it. That step stays in a small
function of the caller.

## Tests

- `sha256` holds the example digests of FIPS 180-4. It also holds one message
  at each length where the pad changes its form: 55, 56, 63, 64 and 65 bytes.
- `hex` holds the examples of RFC 4648 section 10 in lower case, and each
  value of a byte.
- `pytext` compares `is_space` with the full list of the characters that
  Python 3.13 calls space. The list has 29 characters. Four of them are the
  separators U+001C to U+001F, which Rust does not call white space.
- No vector surface records a helper, so this crate has no differential test.
  The differential tests of the callers walk each vector that holds a digest.
  Such a vector holds one of these four values:
  1. The revision of a registry.
  2. The hash of a resolved manifest.
  3. The id of an approval gate.
  4. The digest of the arguments of a call.

## Known gaps

- The owner did not confirm the name `creche-util` and the six rules yet. A
  new name changes the directory, the `name` key, each path dependency and
  each `creche_util::` path.
- The crate holds no base64 yet. The workspace has more than one base64
  decoder and one base64 encoder. Packet `decisions-util-runtime` moves them
  here.
- `pytext::words` has one user module today: `family`. It is here as a part
  of the white space rule, which has users in `config`, `family` and
  `session`. Rule 3 permits that with its sentence on the functions of one
  published rule. The owner confirms that sentence with the six rules. The
  other choice makes `words` a private function of `family`.
- `is_header_space` in `crates/creche-contracts/src/ids.rs` holds the set of
  `pytext::is_space` as a written list. `OwuiChatId::from_header` trims a
  header value with it. It is a second copy of the set. A change to `ids`
  needs the owner of the crate (`rust/AGENTS.md`, "Where a new type goes").
  No packet has this change yet.
- `py_strip` in `crates/agent-family/src/yaml/construct.rs` holds a white
  space set of its own. It trims a text before the reader makes a number of
  that text. The rule for a number is not the rule of `pytext::strip`. The
  module doc of `pytext` gives the difference. No packet changes `py_strip`
  yet.
- `sha256::digest` has no branch and no index that depends on a byte of the
  message. No test and no compiler check proves that its time is constant.
