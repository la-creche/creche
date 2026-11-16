// The files of a prompt (docs/design/attachments.md, sections 3 and 7).
// attendance scanned and rebuilt each file before the prompt came here.
//
//   image block ────┐                              ┌─► WORKSPACE/attachments/<name>
//   resource block ─┴─► fileOf ──► checkFiles ──► save ─┤
//                                                       └─► one marker line each
//
//   a user text of the history ──► splitMarkers ──► the user text and the markers
//                                                └─► readImage: the image again
//
// The marker line is the one record of a file in the history:
// `[attached: attachments/<name> (<type>, <bytes> bytes)]`. The prompt text
// gets one line for each file after the user text and one empty line. This
// module writes the line and reads it back, so the two never differ.
//
// Each constant is equal to `rust/crates/wire/vectors/scan.json`, and
// `test/attachments.test.ts` holds it equal.

import { closeSync, constants, fstatSync, lstatSync, mkdirSync, openSync, readSync, writeFileSync } from "node:fs";
import { extname, join } from "node:path";

import type { ContentBlock, Outcome } from "./acp.js";
import { byteLength, cutToBytes } from "./line.js";
import { MAX_LINE_BYTES } from "./vectors.js";

/** The scheme of the `uri` of a file block. The file name follows it. */
export const URI_SCHEME = "attachment:";

/** The longest file name, in UTF-8 bytes. */
export const MAX_FILE_NAME_BYTES = 255;

/** The most files of one message. A PDF and its text count as one file. */
export const MAX_FILES = 4;

/** The largest file, in bytes. */
export const MAX_FILE_BYTES = 8_388_608;

/** The largest sum of the files of one message, in bytes. */
export const MAX_MESSAGE_BYTES = 10_485_760;

/** The image types: each goes to the model as image input too. */
export const IMAGE_TYPES: readonly string[] = ["image/png", "image/jpeg", "image/gif", "image/webp"];

/** The PDF type. The scanner adds the text of a PDF as `<name>.txt`. */
export const PDF_TYPES: readonly string[] = ["application/pdf"];

/** The text and code types. */
export const TEXT_TYPES: readonly string[] = [
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/html",
  "text/css",
  "text/javascript",
  "text/x-python",
  "text/x-rust",
  "text/x-go",
  "text/x-c",
  "text/x-c++",
  "text/x-java",
  "text/x-typescript",
  "text/x-shellscript",
  "application/json",
  "application/xml",
  "application/yaml",
  "application/toml",
  "application/javascript",
  "application/typescript",
  "application/x-sh",
  "application/sql",
];

/** The directory of the files, in the workspace. The marker names it. */
export const ATTACHMENTS_DIR = "attachments";

/** The text that the scanner adds to the name of a PDF for its text. */
const PDF_TEXT_SUFFIX = ".txt";

/**
 * The bytes of a replayed `image` update that are not its data: the
 * JSON-RPC envelope, the session id, the uri and the type. A replayed image
 * whose data does not fit the line cap with this room replays as a link only.
 */
const IMAGE_LINE_ROOM = 65_536;

/** A control character: C0, DEL or C1. */
const CONTROL = /\p{Cc}/u;

/** A marker line. The name holds no LF, and the type holds no space, comma or parenthesis. */
const MARKER = /^\[attached: attachments\/([^\n]+) \(([^\s,()]+), (\d+) bytes\)\]$/u;

/** How a file reaches the model. */
export enum FileKind {
  /** The file is in the workspace, and the model gets it as image input. */
  Image = "image",
  /** The file is in the workspace only. */
  Workspace = "workspace",
}

/** One checked file of a prompt. */
export interface PromptFile {
  /** The name of the `uri`. */
  readonly name: string;
  readonly mimeType: string;
  readonly kind: FileKind;
  readonly bytes: Buffer;
}

/** One file in the workspace, under the name that it got there. */
export interface SavedFile extends PromptFile {
  /** The unique name in `attachments/`: `name`, or `name` with `-1`, `-2` before its extension. */
  readonly savedName: string;
}

/** One marker line of a user text. */
export interface Marker {
  readonly name: string;
  readonly mimeType: string;
  readonly size: number;
}

function invalid<T>(detail: string): Outcome<T> {
  return { ok: false, refusal: { kind: "invalid_params", detail } };
}

/** The file name of a `uri`, or the rule that it breaks (design section 3). */
export function fileName(uri: string | null | undefined): Outcome<string> {
  if (uri === null || uri === undefined || !uri.startsWith(URI_SCHEME)) {
    return invalid(`a file block needs the uri ${URI_SCHEME}<name>`);
  }

  const name = uri.slice(URI_SCHEME.length);
  if (name.length === 0 || name === "." || name === "..") {
    return invalid("a file name is empty, . or ..");
  }

  if (!name.isWellFormed() || name.includes("/") || CONTROL.test(name)) {
    return invalid("a file name holds a /, a control character or a lone surrogate");
  }

  if (byteLength(name) > MAX_FILE_NAME_BYTES) {
    return invalid(`a file name is over ${MAX_FILE_NAME_BYTES} bytes`);
  }

  return { ok: true, value: name };
}

/** The bytes of a base64 text, or null when the text is not canonical base64. */
function decoded(data: string): Buffer | null {
  const bytes = Buffer.from(data, "base64");

  return bytes.toString("base64") === data ? bytes : null;
}

function checkedFile(name: string, mimeType: string, kind: FileKind, bytes: Buffer | null): Outcome<PromptFile> {
  if (bytes === null) {
    return invalid(`the file ${name} is not base64`);
  }

  if (bytes.length > MAX_FILE_BYTES) {
    return invalid(`the file ${name} is over ${MAX_FILE_BYTES} bytes`);
  }

  return { ok: true, value: { name, mimeType, kind, bytes } };
}

function imageFile(block: Extract<ContentBlock, { type: "image" }>): Outcome<PromptFile> {
  const name = fileName(block.uri);
  if (!name.ok) {
    return name;
  }

  if (!IMAGE_TYPES.includes(block.mimeType)) {
    return invalid(`the facade takes no image of the type ${block.mimeType} (${name.value})`);
  }

  return checkedFile(name.value, block.mimeType, FileKind.Image, decoded(block.data));
}

function resourceFile(block: Extract<ContentBlock, { type: "resource" }>): Outcome<PromptFile> {
  const { resource } = block;
  const name = fileName(resource.uri);
  if (!name.ok) {
    return name;
  }

  const mimeType = resource.mimeType ?? "";
  if (!PDF_TYPES.includes(mimeType) && !TEXT_TYPES.includes(mimeType)) {
    return invalid(`the facade takes no file of the type ${mimeType || "(none)"} (${name.value})`);
  }

  const bytes = "blob" in resource ? decoded(resource.blob) : Buffer.from(resource.text, "utf8");
  return checkedFile(name.value, mimeType, FileKind.Workspace, bytes);
}

/** The file of an `image` or a `resource` block, checked. Null: the block is no file block. */
export function fileOf(block: ContentBlock): Outcome<PromptFile> | null {
  switch (block.type) {
    case "image":
      return imageFile(block);
    case "resource":
      return resourceFile(block);
    default:
      return null;
  }
}

/**
 * The count of the files: the scanner gives a PDF and its text, so a text
 * named `<pdf>.txt` next to the PDF `<pdf>` counts with that PDF.
 */
function fileCount(files: readonly PromptFile[]): number {
  const pdfs = new Set(files.filter((file) => PDF_TYPES.includes(file.mimeType)).map((file) => file.name));

  return files.filter((file) => !(file.name.endsWith(PDF_TEXT_SUFFIX) && pdfs.has(file.name.slice(0, -PDF_TEXT_SUFFIX.length)))).length;
}

/** The limits of one message, again: attendance checked them first (design section 2.4). */
export function checkFiles(files: readonly PromptFile[]): Outcome<readonly PromptFile[]> {
  if (fileCount(files) > MAX_FILES) {
    return invalid(`the prompt has more than ${MAX_FILES} files`);
  }

  const total = files.reduce((sum, file) => sum + file.bytes.length, 0);
  if (total > MAX_MESSAGE_BYTES) {
    return invalid(`the files of the prompt are over ${MAX_MESSAGE_BYTES} bytes`);
  }

  return { ok: true, value: files };
}

/** `name` with `-<count>` before its extension, cut so that it stays a file name. */
function numbered(name: string, count: number): string {
  if (count === 0) {
    return name;
  }

  const extension = extname(name);
  const suffix = `-${count}${extension}`;
  const stem = name.slice(0, name.length - extension.length);

  return `${cutToBytes(stem, MAX_FILE_NAME_BYTES - byteLength(suffix))}${suffix}`;
}

/** The marker line of one saved file. */
export function markerOf(file: SavedFile): string {
  return `[attached: ${ATTACHMENTS_DIR}/${file.savedName} (${file.mimeType}, ${file.bytes.length} bytes)]`;
}

/** The text that goes to Flue: the user text, one empty line, then one marker line for each file. */
export function bodyOf(text: string, files: readonly SavedFile[]): string {
  const markers = files.map(markerOf).join("\n");
  if (markers.length === 0) {
    return text;
  }

  return text.length === 0 ? markers : `${text}\n\n${markers}`;
}

function markerAt(line: string): Marker | null {
  const found = MARKER.exec(line);
  if (found === null) {
    return null;
  }

  const [, name = "", mimeType = "", size = ""] = found;
  return { name, mimeType, size: Number(size) };
}

/**
 * The user text and the markers of a text of `bodyOf`. The markers are the
 * last lines, after an empty line or at the start. A text with no such
 * block is all user text.
 */
export function splitMarkers(body: string): { readonly text: string; readonly markers: readonly Marker[] } {
  const lines = body.split("\n");
  let start = lines.length;
  while (start > 0 && markerAt(lines[start - 1] ?? "") !== null) {
    start -= 1;
  }

  const markers = lines.slice(start).map((line) => markerAt(line)).filter((marker) => marker !== null);
  if (markers.length === 0 || (start > 0 && lines[start - 1] !== "")) {
    return { text: body, markers: [] };
  }

  return { text: lines.slice(0, Math.max(0, start - 1)).join("\n"), markers };
}

/** The workspace directory of the files. */
export class Attachments {
  private readonly dir: string;

  /** `workspace` is `WORKSPACE` of the guest start (`agent/src/guest.ts`). */
  public constructor(workspace: string) {
    this.dir = join(workspace, ATTACHMENTS_DIR);
  }

  /**
   * Writes each file under a name that no file has yet. The agent can write
   * the workspace, so the directory must be a real directory, and each
   * write makes a new file: it follows no link and replaces no file.
   * Throws on a fault of the file system.
   */
  public save(files: readonly PromptFile[]): SavedFile[] {
    if (files.length === 0) {
      return [];
    }

    mkdirSync(this.dir, { recursive: true });
    if (!lstatSync(this.dir).isDirectory()) {
      throw new Error(`${ATTACHMENTS_DIR} in the workspace is not a directory`);
    }

    return files.map((file) => ({ ...file, savedName: this.saveOne(file) }));
  }

  private saveOne(file: PromptFile): string {
    for (let count = 0; ; count += 1) {
      const savedName = numbered(file.name, count);
      try {
        writeFileSync(join(this.dir, savedName), file.bytes, { flag: "wx" });
        return savedName;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
          throw error;
        }
      }
    }
  }

  /**
   * The base64 of the image of `marker`, or null: the type is no image
   * type, the workspace holds no regular file of the name, or the data is
   * too large for one line.
   */
  public readImage(marker: Marker): string | null {
    const name = fileName(`${URI_SCHEME}${marker.name}`);
    if (!name.ok || !IMAGE_TYPES.includes(marker.mimeType)) {
      return null;
    }

    try {
      return this.readSmall(name.value);
    } catch {
      return null;
    }
  }

  /** The base64 of a regular file that fits one line, or null. Throws when the file does not open. */
  private readSmall(name: string): string | null {
    const fd = openSync(join(this.dir, name), constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const stat = fstatSync(fd);
      const { size } = stat;
      if (!stat.isFile() || base64Bytes(size) + IMAGE_LINE_ROOM > MAX_LINE_BYTES) {
        return null;
      }

      const bytes = Buffer.alloc(size);
      const read = readSync(fd, bytes, 0, size, 0);
      return bytes.subarray(0, read).toString("base64");
    } finally {
      closeSync(fd);
    }
  }
}

/** The length of the base64 of `size` bytes. */
function base64Bytes(size: number): number {
  return Math.ceil(size / 3) * 4;
}
