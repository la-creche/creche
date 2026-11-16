// The files of a prompt: the constants equal to the scan vectors, the name
// rule, the unique names in the workspace and the marker lines. Never
// change the vector file to make this test pass: change the file and each
// package's copy together.

import { mkdtempSync, mkdirSync, readFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import {
  Attachments,
  bodyOf,
  checkFiles,
  FileKind,
  fileName,
  IMAGE_TYPES,
  MAX_FILE_BYTES,
  MAX_FILE_NAME_BYTES,
  MAX_FILES,
  MAX_MESSAGE_BYTES,
  PDF_TYPES,
  splitMarkers,
  TEXT_TYPES,
  URI_SCHEME,
} from "../src/attachments.js";
import type { PromptFile } from "../src/attachments.js";

const SCAN_FILE = new URL("../../rust/crates/wire/vectors/scan.json", import.meta.url);

const scan = JSON.parse(readFileSync(SCAN_FILE, "utf8")) as {
  acp_line_cap: number;
  max_files: number;
  max_file_bytes: number;
  max_message_bytes: number;
  uri_scheme: string;
  max_name_bytes: number;
  kinds: { image: string[]; pdf: string[]; text: string[] };
};

function file(name: string, mimeType = "text/plain", bytes = Buffer.from("hello\n")): PromptFile {
  return { name, mimeType, kind: FileKind.Workspace, bytes };
}

function workspace(): string {
  return mkdtempSync(join(tmpdir(), "facade-attachments-"));
}

describe("the scan values", () => {
  it("hold each limit, the scheme and the kinds of the scan vectors", () => {
    expect(MAX_FILES).toBe(scan.max_files);
    expect(MAX_FILE_BYTES).toBe(scan.max_file_bytes);
    expect(MAX_MESSAGE_BYTES).toBe(scan.max_message_bytes);
    expect(URI_SCHEME).toBe(scan.uri_scheme);
    expect(MAX_FILE_NAME_BYTES).toBe(scan.max_name_bytes);
    expect(IMAGE_TYPES).toEqual(scan.kinds.image);
    expect(PDF_TYPES).toEqual(scan.kinds.pdf);
    expect(TEXT_TYPES).toEqual(scan.kinds.text);
  });

  it("give the ACP line cap of the scan vectors, and initialize offers images and embedded files", () => {
    expect(vectors.max_line_bytes).toBe(scan.acp_line_cap);
    expect(vectors.initialize.response.result.agentCapabilities.promptCapabilities).toEqual({ image: true, embeddedContext: true });
  });
});

describe("fileName", () => {
  it("takes a name of the attachment scheme", () => {
    expect(fileName("attachment:notes (1).txt")).toEqual({ ok: true, value: "notes (1).txt" });
    expect(fileName(`attachment:${"é".repeat(127)}a`)).toMatchObject({ ok: true });
  });

  it.each([
    ["no uri", undefined],
    ["another scheme", "file:///etc/passwd"],
    ["an empty name", "attachment:"],
    ["a dot", "attachment:."],
    ["two dots", "attachment:.."],
    ["a slash", "attachment:../flue.db"],
    ["a NUL", "attachment:a\u0000b"],
    ["a LF", "attachment:a\nb"],
    ["a C1 control", "attachment:a\u0085b"],
    ["a lone surrogate", "attachment:a\ud800"],
    ["256 bytes", `attachment:${"a".repeat(256)}`],
  ])("refuses %s", (_case, uri) => {
    expect(fileName(uri)).toMatchObject({ ok: false, refusal: { kind: "invalid_params" } });
  });
});

describe("checkFiles", () => {
  it("takes the limits of one message, and a PDF with its text as one file", () => {
    const pdfs = ["a", "b", "c", "d"].flatMap((name) => [file(`${name}.pdf`, "application/pdf"), file(`${name}.pdf.txt`)]);
    const full = [file("a.txt", "text/plain", Buffer.alloc(MAX_FILE_BYTES)), file("b.txt", "text/plain", Buffer.alloc(MAX_MESSAGE_BYTES - MAX_FILE_BYTES))];

    expect(checkFiles(pdfs)).toMatchObject({ ok: true });
    expect(checkFiles(full)).toMatchObject({ ok: true });
  });

  it("refuses five files, and files over the message limit in all", () => {
    const five = ["a", "b", "c", "d", "e"].map((name) => file(`${name}.txt`));
    const over = [file("a.txt", "text/plain", Buffer.alloc(MAX_FILE_BYTES)), file("b.txt", "text/plain", Buffer.alloc(MAX_MESSAGE_BYTES - MAX_FILE_BYTES + 1))];

    expect(checkFiles(five)).toMatchObject({ ok: false, refusal: { kind: "invalid_params" } });
    expect(checkFiles(over)).toMatchObject({ ok: false, refusal: { kind: "invalid_params" } });
  });
});

describe("Attachments.save", () => {
  it("writes each file under a name that no file has yet", () => {
    const root = workspace();
    const files = new Attachments(root);
    const saved = files.save([file("notes.txt"), file("notes.txt"), file(".env"), file(".env"), file("README")]);
    const again = files.save([file("notes.txt"), file("README")]);

    expect(saved.map((one) => one.savedName)).toEqual(["notes.txt", "notes-1.txt", ".env", ".env-1", "README"]);
    expect(again.map((one) => one.savedName)).toEqual(["notes-2.txt", "README-1"]);
    expect(readFileSync(join(root, "attachments", "notes-1.txt"), "utf8")).toBe("hello\n");
  });

  it("keeps a numbered name within the name limit", () => {
    const long = `${"a".repeat(MAX_FILE_NAME_BYTES - 4)}.txt`;
    const files = new Attachments(workspace());
    const [, second] = files.save([file(long), file(long)]);

    expect(Buffer.byteLength(second?.savedName ?? "")).toBe(MAX_FILE_NAME_BYTES);
    expect(second?.savedName.endsWith("-1.txt")).toBe(true);
  });

  it("follows no link that the agent put in place of a name", () => {
    const root = workspace();
    mkdirSync(join(root, "attachments"));
    symlinkSync(join(root, "elsewhere"), join(root, "attachments", "notes.txt"));
    const [saved] = new Attachments(root).save([file("notes.txt")]);

    expect(saved?.savedName).toBe("notes-1.txt");
  });

  it("refuses a link in place of the directory", () => {
    const root = workspace();
    mkdirSync(join(root, "other"));
    symlinkSync(join(root, "other"), join(root, "attachments"));

    expect(() => new Attachments(root).save([file("notes.txt")])).toThrow(/not a directory/);
  });
});

describe("the marker lines", () => {
  it("round trip the user text and each file", () => {
    const files = new Attachments(workspace()).save([file("a b (1).txt"), file("cat.png", "image/png", Buffer.alloc(3))]);

    for (const text of ["Look.", "Two\n\nparagraphs.\n", ""]) {
      const split = splitMarkers(bodyOf(text, files));

      expect(split.text).toBe(text);
      expect(split.markers).toEqual([
        { name: "a b (1).txt", mimeType: "text/plain", size: 6 },
        { name: "cat.png", mimeType: "image/png", size: 3 },
      ]);
    }
  });

  it("give the text as it is when it has no file", () => {
    expect(bodyOf("Hi.", [])).toBe("Hi.");
    expect(splitMarkers("Hi.")).toEqual({ text: "Hi.", markers: [] });
  });

  it("names the file in the body", () => {
    const files = new Attachments(workspace()).save([file("cat.png", "image/png", Buffer.alloc(3))]);

    expect(bodyOf("Look.", files)).toBe("Look.\n\n[attached: attachments/cat.png (image/png, 3 bytes)]");
  });
});

describe("Attachments.readImage", () => {
  it("reads an image that the workspace holds, and nothing else", () => {
    const root = workspace();
    const files = new Attachments(root);
    files.save([file("cat.png", "image/png", Buffer.from("png")), file("notes.txt")]);
    symlinkSync(join(root, "attachments", "cat.png"), join(root, "attachments", "link.png"));

    expect(files.readImage({ name: "cat.png", mimeType: "image/png", size: 3 })).toBe(Buffer.from("png").toString("base64"));
    expect(files.readImage({ name: "notes.txt", mimeType: "text/plain", size: 6 })).toBeNull();
    expect(files.readImage({ name: "link.png", mimeType: "image/png", size: 3 })).toBeNull();
    expect(files.readImage({ name: "gone.png", mimeType: "image/png", size: 1 })).toBeNull();
    expect(files.readImage({ name: "../cat.png", mimeType: "image/png", size: 3 })).toBeNull();
  });
});
