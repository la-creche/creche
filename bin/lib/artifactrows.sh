# Sourced only (bin/AGENTS.md, Library): the one reader of the line form of
# `handover artifacts`. bin/build-artifacts.sh and bin/artifact-probe.sh
# source it, so the two scripts cannot read one row in two ways.
#
# The line form is `ArtifactRow.lines` of handover/src/handover/artifacts.py.
# A row is these lines, in this order:
#
#   component <name>            the component, or the crate of a probe row
#   target <name>               the target of the build
#   archive <name>              the name of the archive
#   bundle <name>               the name of its signature bundle
#   install-env <variable>      the variable that names the install root
#   max-archive-bytes <count>   the bytes of the archive, at most
#   max-members <count>         the members of the archive, at most
#   max-unpacked-bytes <count>  the bytes of the tree, at most
#   argv <count>                one command: the count of its words, then
#   <word>                      one word on each line. A row has one
#   ...                         command or more.
#   end
#
# `read_rows FILE` reads each row of FILE into the arrays below. It refuses
# each other text: a key out of its place, a line that is no key of the
# form, a name that is not plain, a limit that is no count above 0, a row
# with no command or with no `end`, and a text after the last `end`. It
# also refuses two rows with one component, one archive or one bundle. A
# refusal returns 1 and puts the reason into ROWS_FAULT. After a refusal,
# the arrays can hold rows. A caller uses none of them.
#
# The reader exits no shell. Each caller says what a refusal stops, so call
# it as `read_rows FILE || ...`. Each check returns by itself, so the reader
# holds with `set -e` and without it.
#
# Bash 3.2-clean: the tests of the two scripts run it on a development
# machine.

#: The keys of a row before its first command, in their order.
HEAD_KEYS="component target archive bundle install-env max-archive-bytes max-members max-unpacked-bytes"

#: The grammar of a name that goes into a path or a command line: it starts
#: with a letter or a digit, so no program reads it as an option.
PLAIN_NAME='^[A-Za-z0-9][A-Za-z0-9._-]*$'

#: The grammar of the name of a variable, of a limit and of a word count.
VARIABLE_NAME='^[A-Za-z_][A-Za-z0-9_]*$'
LIMIT='^[1-9][0-9]{0,17}$'
WORD_COUNT='^[1-9][0-9]{0,3}$'

#: One entry for each row. ROW_FIRST is the index of the first command of
#: the row in COMMAND_FIRST, and ROW_COMMANDS is its count of commands.
ROW_COMPONENT=()
ROW_TARGET=()
ROW_ARCHIVE=()
ROW_BUNDLE=()
ROW_ENV=()
ROW_MAX_ARCHIVE=()
ROW_MAX_MEMBERS=()
ROW_MAX_UNPACKED=()
ROW_FIRST=()
ROW_COMMANDS=()

#: One entry for each command: the index of its first word in COMMAND_WORD,
#: and its count of words. COMMAND_WORD holds each word of each command.
COMMAND_FIRST=()
COMMAND_COUNT=()
COMMAND_WORD=()

#: The reason of the last refusal.
ROWS_FAULT=""

#: The lines of the file, the index of the next line, that line and the
#: value of its key.
ROWS_LINES=()
ROWS_POS=0
ROWS_LINE=""
ROWS_VALUE=""

# rows_next: sets ROWS_LINE to the next line. Status 1 when the lines end
# there, inside a row.
rows_next() {
  if [[ "$ROWS_POS" -ge "${#ROWS_LINES[@]}" ]]; then
    ROWS_FAULT="the rows end inside a row"
    return 1
  fi

  ROWS_LINE="${ROWS_LINES[$ROWS_POS]}"
  ROWS_POS=$((ROWS_POS + 1))
}

# rows_value KEY: sets ROWS_VALUE to the value of the next line. Status 1
# when that line has no key KEY.
rows_value() {
  rows_next || return 1
  if [[ "$ROWS_LINE" != "$1 "* ]]; then
    ROWS_FAULT="a row has no key $1 where the line form puts it"
    return 1
  fi

  ROWS_VALUE="${ROWS_LINE#"$1 "}"
}

# rows_head: reads the keys of a row before its first command.
rows_head() {
  local key

  for key in $HEAD_KEYS; do
    rows_value "$key" || return 1
    case "$key" in
      component | target | archive | bundle)
        if [[ ! "$ROWS_VALUE" =~ $PLAIN_NAME ]]; then
          ROWS_FAULT="the $key of a row is not a plain name"
          return 1
        fi
        ;;
      install-env)
        if [[ ! "$ROWS_VALUE" =~ $VARIABLE_NAME ]]; then
          ROWS_FAULT="the install-env of a row is not a variable name"
          return 1
        fi
        ;;
      *)
        if [[ ! "$ROWS_VALUE" =~ $LIMIT ]]; then
          ROWS_FAULT="the $key of a row is not a count above 0"
          return 1
        fi
        ;;
    esac

    case "$key" in
      component) ROW_COMPONENT+=("$ROWS_VALUE") ;;
      target) ROW_TARGET+=("$ROWS_VALUE") ;;
      archive) ROW_ARCHIVE+=("$ROWS_VALUE") ;;
      bundle) ROW_BUNDLE+=("$ROWS_VALUE") ;;
      install-env) ROW_ENV+=("$ROWS_VALUE") ;;
      max-archive-bytes) ROW_MAX_ARCHIVE+=("$ROWS_VALUE") ;;
      max-members) ROW_MAX_MEMBERS+=("$ROWS_VALUE") ;;
      max-unpacked-bytes) ROW_MAX_UNPACKED+=("$ROWS_VALUE") ;;
    esac
  done
}

# rows_commands: reads the commands of a row and its `end` line. A row has
# one command or more, and the first word of a command is not empty.
rows_commands() {
  local count first index commands=0

  ROW_FIRST+=("${#COMMAND_FIRST[@]}")
  while true; do
    rows_next || return 1
    if [[ "$ROWS_LINE" == "end" && "$commands" -gt 0 ]]; then
      break
    fi

    if [[ "$ROWS_LINE" != "argv "* ]]; then
      ROWS_FAULT="a row has no command where the line form puts one"
      return 1
    fi
    count="${ROWS_LINE#argv }"
    if [[ ! "$count" =~ $WORD_COUNT ]]; then
      ROWS_FAULT="a command of a row has no count of words"
      return 1
    fi

    first="${#COMMAND_WORD[@]}"
    COMMAND_FIRST+=("$first")
    COMMAND_COUNT+=("$count")
    for ((index = 0; index < count; index++)); do
      rows_next || return 1
      COMMAND_WORD+=("$ROWS_LINE")
    done
    if [[ -z "${COMMAND_WORD[$first]}" ]]; then
      ROWS_FAULT="a command of a row has an empty first word"
      return 1
    fi
    commands=$((commands + 1))
  done
  ROW_COMMANDS+=("$commands")
}

# read_rows FILE: reads each row of FILE. Status 1 with the reason in
# ROWS_FAULT for a file that it cannot read and for each text that is not in
# the line form. Two rows with one component, one archive or one bundle are
# refused too: the second archive would replace the first.
read_rows() {
  local line row other

  ROW_COMPONENT=()
  ROW_TARGET=()
  ROW_ARCHIVE=()
  ROW_BUNDLE=()
  ROW_ENV=()
  ROW_MAX_ARCHIVE=()
  ROW_MAX_MEMBERS=()
  ROW_MAX_UNPACKED=()
  ROW_FIRST=()
  ROW_COMMANDS=()
  COMMAND_FIRST=()
  COMMAND_COUNT=()
  COMMAND_WORD=()
  ROWS_FAULT=""
  ROWS_LINES=()

  if [[ ! -f "$1" || ! -r "$1" ]]; then
    ROWS_FAULT="cannot read the file of the rows"
    return 1
  fi
  # The last line counts also when no line feed ends it.
  while IFS= read -r line || [[ -n "$line" ]]; do
    ROWS_LINES+=("$line")
  done < "$1"

  ROWS_POS=0
  while [[ "$ROWS_POS" -lt "${#ROWS_LINES[@]}" ]]; do
    rows_head || return 1
    rows_commands || return 1
  done

  for ((row = 0; row < ${#ROW_COMPONENT[@]}; row++)); do
    for ((other = 0; other < row; other++)); do
      if [[ "${ROW_COMPONENT[$other]}" == "${ROW_COMPONENT[$row]}" ]]; then
        ROWS_FAULT="two rows name the component ${ROW_COMPONENT[$row]}"
        return 1
      fi
      if [[ "${ROW_ARCHIVE[$other]}" == "${ROW_ARCHIVE[$row]}" ]]; then
        ROWS_FAULT="two rows name the archive ${ROW_ARCHIVE[$row]}"
        return 1
      fi
      if [[ "${ROW_BUNDLE[$other]}" == "${ROW_BUNDLE[$row]}" ]]; then
        ROWS_FAULT="two rows name the bundle ${ROW_BUNDLE[$row]}"
        return 1
      fi
    done
  done
}
