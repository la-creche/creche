"""A JSON parser that refuses each document as too deep.

`json.loads` raises RecursionError on a document that nests too deep. The
depth that it refuses differs between Python versions. Python 3.14 reads each
depth that fits under the size cap of some readers of this door. A test for
such a reader gives the reader this parser in place of the `json` module.
"""

from __future__ import annotations


class ParserAtItsLimit:
    """Stands for the `json` module of a reader. `loads` refuses each text."""

    @staticmethod
    def loads(_text: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")
