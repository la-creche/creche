# Attachments: files in a prompt

Status: DECIDED 2026-11-15. The operator gave each answer of section 2.

A door client can attach a file to a `session/prompt`. A scanner service
VM checks and rebuilds each file before any family VM gets it. The family
agent then reads the file in its workspace, and the model sees each image.

## 1. The path of a file

```
door client ──session/prompt: image and resource blocks──► attendance door
                                                              │
                     one link per file, kind line "scan"      │
   scanner VM ◄── VMM_SCANNER_SOCKET ◄── vmm ◄────────────────┘
      │  1 true type (Magika)   2 known malware (ClamAV)
      │  3 rebuild: image re-encode, PDF from page pixels, text UTF-8
      └──► rebuilt files ──► attendance door ──► family VM facade
                                                   │
                     WORKSPACE/attachments/<name> ◄┤
                     image input of the model    ◄─┘
```

## 2. The decisions

1. **Kinds.** Images, PDF, text and code. Audio, video, archives and other
   binary files are refused.
2. **The scan.** A scanner service VM runs the scan. Only rebuilt bytes
   reach a family VM. The host parses no byte of a file.
3. **The transport.** Each file lands in the session workspace. The prompt
   text names its path. Each image also goes to the model as image input.
4. **The size.** A file is 8 MiB or less. The files of one message are
   10 MiB or less in all. A message has 4 files or less. A PDF has 50
   pages or less. The ACP line cap is 16 MiB.
5. **The history.** The workspace keeps the bytes. Flue keeps its own copy
   of each image that the model sees. The history keeps the name and the
   path of each file. `session/load` replays each file as a
   `resource_link`, and each image as an `image` block that the facade
   reads from the workspace.
6. **The models.** Each alias of the router takes image input. The
   operator checks each model in a separate task.

`rust/crates/wire/vectors/scan.json` holds every number and every list of
this note. Each package holds its constants equal to that file in a test.

## 3. The blocks of a prompt

A door client sends each file as one block of `session/prompt`:

1. An image is `{"type": "image", "mimeType", "data", "uri"}`.
2. Any other file is `{"type": "resource", "resource": {"uri",
   "mimeType", "blob"}}`. A `text` resource is the UTF-8 bytes of its
   text.
3. The `uri` is `attachment:` and the file name. The name is 255 bytes or
   less. It has no `/`, no NUL and no control character, and it is not
   `.` or `..`.

`initialize` of the facade gives `promptCapabilities` `image: true` and
`embeddedContext: true`. So the door client knows that it may send both
blocks.

## 4. The scan link

The vmm serves `VMM_SCANNER_SOCKET`, as it serves the browser socket. The
peer user is the user of attendance. The vmm writes the kind line `scan`
on each guest link, then joins the link to the scanner facade.

1. attendance opens one link for each file.
2. attendance sends one JSON line: `{"name", "mime_type", "data"}`. The
   `data` is base64.
3. The scanner sends one JSON line, then closes the link:
   - `{"ok": {"files": [{"name", "mime_type", "data"}]}}`, or
   - `{"refused": {"reason": <reason>}}`.
4. A line is `line_cap` bytes or less in each direction.
5. attendance waits `wait_s` seconds for the reply. Then it closes the
   link, and the reason is `failed`.

| Reason | Cause |
|---|---|
| `not_allowed` | the claimed type is not in `kinds` |
| `mismatch` | the bytes are not of the claimed kind |
| `malware` | ClamAV found a signature |
| `too_large` | over a byte, pixel or page limit |
| `unreadable` | the decoder failed |
| `not_utf8` | a text file is not strict UTF-8, or has a NUL byte |
| `busy` | the socket holds its most links |
| `failed` | the scanner did not answer, or answered a bad line |

## 5. The scanner VM

The scanner VM is the service image `scanner`. It has no network device
and no egress rule. Its ClamAV signatures come with the image, so a new
image release gives new signatures. The VM has 2 vCPUs and 3072 MiB.

For each file, the scanner runs these steps in this sequence:

1. It finds the kind of the claimed type in `kinds`. No kind is
   `not_allowed`.
2. Magika reads the bytes. A label of another kind is `mismatch`.
3. `clamd` scans the bytes. A signature is `malware`.
4. It rebuilds the file:
   - An image is decoded with a pixel limit and encoded again. JPEG
     stays JPEG. Each other image becomes PNG. No metadata stays.
   - A PDF is rendered page by page to pixels at 150 dpi, and a new PDF
     is made from the pixels. The text of the PDF goes into a second
     file, `<name>.txt`.
   - A text file must be strict UTF-8 with no NUL byte. Its bytes stay.
5. It answers the rebuilt files.

The scan does not look for prompt injection. No library finds it
reliably. The permission layer of the chaperone stays the guard of what
the agent can do.

## 6. The door step

1. A `session/prompt` with no image and no resource block goes to the
   family VM as before.
2. Each other prompt is checked for `max_files`, `max_file_bytes`,
   `max_message_bytes` and the `uri` rule. A failed check gets
   `invalidParams` (-32602) and reaches no stream.
3. The door scans each file. A refusal gets `invalidParams`. The message
   names the file and the reason.
4. The door puts the rebuilt files in the place of the blocks. A rebuilt
   image is an `image` block. Each other rebuilt file is a `resource`
   block.
5. A `session/cancel` while the door scans gets the prompt answered with
   the stop reason `cancelled`. The prompt reaches no stream.
6. When attendance has no scanner socket set, each prompt with a file
   gets `invalidParams`. A prompt with no file goes to the family VM.

## 7. The facade step

1. The facade writes each file to `WORKSPACE/attachments/<name>`. A name
   that is there already gets `-1`, `-2` and so on before its extension.
2. Each image also goes to Flue as an attachment of the user message.
3. The prompt text gets one line for each file, after the text of the
   user: `[attached: attachments/<name> (<type>, <bytes> bytes)]`.
4. The replay finds these lines in a user message. It shows the user text
   without them, then one `resource_link` for each file. An image that the
   workspace holds also replays as an `image` block.

## 8. The host steps

The operator does these steps once, before the first file:

1. Make the socket dir as root: owner the vmm user, group the attendance
   user, mode 2750.
2. Set `VMM_SCANNER_SOCKET` and `VMM_SCANNER_PEER_USER` for the vmm, and
   the scanner socket for attendance.
3. Release the disks row with the `scanner` service image.
