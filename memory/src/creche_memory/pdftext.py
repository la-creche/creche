"""The text of one PDF, read in a child process: `python -I -m creche_memory.pdftext <path>`.

A PDF in the vault comes from a phone, a Mac or an agent, so its parser is
untrusted input. The memory service holds the LiteLLM key and runs for days.
So the service never parses a PDF itself. It starts this module for each PDF,
with no env, a time limit, and on Linux a memory and a CPU limit. A crash or
a hang here costs one PDF, never the service.

The child writes the text as UTF-8 on stdout, at most `MAX_PDF_CHARS + 1`
characters. One character more than the cap tells the parent that the PDF
was cut. Any error exits non-zero and writes nothing the parent reads."""

import sys

#: The characters of one PDF that the memory store takes (David, 2026-11-03).
MAX_PDF_CHARS = 200_000

#: The address space of the child, on Linux. pypdf on a large statement
#: stays well under it. A crafted PDF that asks for more fails here.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

#: The CPU seconds of the child, on Linux. The parent's wall-time limit is
#: longer, so this limit ends a busy loop first.
CPU_LIMIT_S = 60


def _limit() -> None:
    if sys.platform != "linux":
        return

    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_S, CPU_LIMIT_S))


def read_text(path: str) -> str:
    """The text of the PDF at `path`, page by page, cut at one character
    over the cap."""
    from pypdf import PdfReader

    pages: list[str] = []
    size = 0
    for page in PdfReader(path).pages:
        text = page.extract_text() or ""
        pages.append(text)
        size += len(text) + 2
        if size > MAX_PDF_CHARS:
            break

    return "\n\n".join(pages)[: MAX_PDF_CHARS + 1]


def main() -> int:
    if len(sys.argv) != 2:
        return 2

    _limit()
    sys.stdout.buffer.write(read_text(sys.argv[1]).encode("utf-8"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
