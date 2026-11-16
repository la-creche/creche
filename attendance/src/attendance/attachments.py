"""The files of a `session/prompt` at the ACP door (design note attachments
§3, §6).

A door client sends each file as one block of the prompt. An `image` block
is `{"type": "image", "mimeType", "data", "uri"}`. Each other file is a
`resource` block, `{"type": "resource", "resource": {"uri", "mimeType",
"blob" | "text"}}`. The `uri` is `attachment:` and the file name.

The door step, for a prompt with one file block or more:

    check   max_files, max_file_bytes, max_message_bytes, the uri rule
    scan    one link for each file, in sequence (`scanner.py`)
    rebuild each file block -> the blocks of its rebuilt files, in place

A rebuilt file of an image type is an `image` block. Each other rebuilt
file is a `resource` block with a `blob`. So a PDF becomes two resource
blocks: the PDF and its `.txt`. Each other block stays in place, in order.
An `audio` block is refused: audio is not an allowed kind. Each failure
raises `AttachmentRefused`, which the door answers with `invalidParams`.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Final

from .atomic import as_array, as_object
from .scanner import (
    IMAGE_TYPES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_MESSAGE_BYTES,
    URI_SCHEME,
    Reason,
    Rebuilt,
    Refused,
    ScanFile,
    Scanner,
    decode_base64,
    is_file_name,
    least_decoded_bytes,
)

BLOCK_IMAGE: Final = "image"
BLOCK_RESOURCE: Final = "resource"

#: A block of a kind that the door refuses (design note attachments
#: decision 1).
BLOCK_AUDIO: Final = "audio"

#: The block types that carry a file.
FILE_BLOCKS: Final = frozenset({BLOCK_IMAGE, BLOCK_RESOURCE})

_PROMPT: Final = "prompt"


class AttachmentRefused(Exception):
    """A prompt whose files may not pass. The text names the rule and the
    file."""


def check_no_audio(params: dict[str, Any] | None) -> None:
    """Design note attachments decision 1: audio is not an allowed kind.
    Raises `AttachmentRefused` that names the first `audio` block."""
    blocks = as_array(params.get(_PROMPT)) if params is not None else None

    for spot, block in enumerate(blocks or [], start=1):
        fields = as_object(block)

        if fields is not None and fields.get("type") == BLOCK_AUDIO:
            raise AttachmentRefused(f"audio: block {spot} is audio, which is not an allowed kind")


def has_files(params: dict[str, Any] | None) -> bool:
    """True when the prompt of `params` holds an `image` or a `resource`
    block. Each other prompt goes to the stream as it is."""
    blocks = as_array(params.get(_PROMPT)) if params is not None else None
    return blocks is not None and any(_is_file_block(one) for one in blocks)


def read_files(params: dict[str, Any]) -> list[ScanFile]:
    """The file of each file block, in order. Raises `AttachmentRefused`."""
    blocks = as_array(params.get(_PROMPT)) or []
    file_blocks = [(spot, one) for spot, one in enumerate(blocks, start=1) if _is_file_block(one)]

    if len(file_blocks) > MAX_FILES:
        raise AttachmentRefused(f"max_files: a message has {MAX_FILES} files or less")

    files = [_file_of(spot, block) for spot, block in file_blocks]

    if sum(len(one.data) for one in files) > MAX_MESSAGE_BYTES:
        raise AttachmentRefused(
            f"max_message_bytes: the files of a message are {MAX_MESSAGE_BYTES} bytes or less"
        )

    return files


async def scan_files(scanner: Scanner, files: list[ScanFile]) -> list[Rebuilt]:
    """Scan each file in sequence. The first refusal raises
    `AttachmentRefused` with the text `<name>: <reason>`.

    The caps hold for the rebuilt files too: each rebuilt file is
    `MAX_FILE_BYTES` or less, and the rebuilt files of the message are
    `MAX_MESSAGE_BYTES` or less in all. Past a cap the reason is
    `too_large`, and the text names the file that the client sent. The
    reply of one file is one file, or a PDF and its `.txt` (`scanner.py`),
    so the count of `MAX_FILES` holds as the facade counts it."""
    rebuilt: list[Rebuilt] = []
    total = 0

    for one in files:
        result = await scanner.scan(one)

        if isinstance(result, Refused):
            raise AttachmentRefused(f"{one.name}: {result.reason}")

        sizes = [len(made.data) for made in result.files]
        total += sum(sizes)

        if max(sizes) > MAX_FILE_BYTES or total > MAX_MESSAGE_BYTES:
            raise AttachmentRefused(f"{one.name}: {Reason.TOO_LARGE}")

        rebuilt.append(result)

    return rebuilt


def rebuilt_line(record: dict[str, Any], rebuilt: list[Rebuilt], cap: int) -> str:
    """The prompt line with the blocks of the rebuilt files in the place of
    each file block. Raises `AttachmentRefused` for a line over `cap`."""
    params = as_object(record.get("params")) or {}
    results = iter(rebuilt)
    blocks: list[Any] = []

    for block in as_array(params.get(_PROMPT)) or []:
        if not _is_file_block(block):
            blocks.append(block)
            continue

        blocks.extend(_block_of(one) for one in next(results).files)

    line = json.dumps(
        {**record, "params": {**params, _PROMPT: blocks}},
        separators=(",", ":"),
        ensure_ascii=False,
    )

    if len(line.encode("utf-8", "surrogatepass")) > cap:
        raise AttachmentRefused(f"acp_line_cap: the rebuilt prompt is over {cap} bytes")

    return line


def _is_file_block(block: object) -> bool:
    fields = as_object(block)
    return fields is not None and fields.get("type") in FILE_BLOCKS


def _file_of(spot: int, block: dict[str, Any]) -> ScanFile:
    """The file of one block. `spot` counts the blocks of the prompt from 1,
    so a refusal can name a block whose name breaks the rule."""
    fields = block if block.get("type") == BLOCK_IMAGE else as_object(block.get(BLOCK_RESOURCE))

    if fields is None:
        raise AttachmentRefused(f"resource: block {spot} holds no resource object")

    name = _name_of(spot, fields.get("uri"))
    mime_type = fields.get("mimeType")

    if not isinstance(mime_type, str) or not mime_type:
        raise AttachmentRefused(f"mimeType: {name} has no mimeType")

    data = _image_data(name, fields) if fields is block else _resource_data(name, fields)

    if len(data) > MAX_FILE_BYTES:
        raise AttachmentRefused(f"max_file_bytes: {name} is over {MAX_FILE_BYTES} bytes")

    return ScanFile(name, mime_type, data)


def _name_of(spot: int, uri: object) -> str:
    """The name of the `uri` rule: `attachment:` and a name of the name
    rule (design note attachments §3 rule 3)."""
    name = uri.removeprefix(URI_SCHEME) if isinstance(uri, str) else None

    if name is None or name == uri or not is_file_name(name):
        raise AttachmentRefused(f"uri: the uri of block {spot} is not {URI_SCHEME} and a file name")

    return name


def _image_data(name: str, fields: dict[str, Any]) -> bytes:
    return _base64_data(name, fields.get("data"), "data")


def _resource_data(name: str, fields: dict[str, Any]) -> bytes:
    """The bytes of a `blob` (base64) or of a `text` (its UTF-8). A
    resource holds one of the two."""
    blob = fields.get("blob")
    text = fields.get("text")

    if (blob is None) == (text is None):
        raise AttachmentRefused(f"resource: {name} holds a blob or a text, and not both")

    if blob is not None:
        return _base64_data(name, blob, "blob")

    if not isinstance(text, str):
        raise AttachmentRefused(f"resource: the text of {name} is not a string")

    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AttachmentRefused(f"resource: the text of {name} has no UTF-8 form") from error


def _base64_data(name: str, value: object, key: str) -> bytes:
    """Strict base64. A text over `MAX_FILE_BYTES` is refused before the
    decode."""
    if not isinstance(value, str):
        raise AttachmentRefused(f"{key}: the {key} of {name} is not a string")

    if least_decoded_bytes(value) > MAX_FILE_BYTES:
        raise AttachmentRefused(f"max_file_bytes: {name} is over {MAX_FILE_BYTES} bytes")

    data = decode_base64(value)

    if data is None:
        raise AttachmentRefused(f"{key}: the {key} of {name} is not strict base64")

    return data


def _block_of(file: ScanFile) -> dict[str, Any]:
    """The ACP block of one rebuilt file."""
    uri = URI_SCHEME + file.name
    data = base64.b64encode(file.data).decode("ascii")

    if file.mime_type in IMAGE_TYPES:
        return {"type": BLOCK_IMAGE, "mimeType": file.mime_type, "data": data, "uri": uri}

    resource = {"uri": uri, "mimeType": file.mime_type, "blob": data}
    return {"type": BLOCK_RESOURCE, "resource": resource}
