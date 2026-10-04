"""Read an HTML page back as a tree, the way a browser does.

A scenario of the noticeboard asserts on what a page holds: a row of a
table, a link, a field of a form. It never compares a whole page with a text.
A template in another language puts other spaces and other line ends around
the same elements, and a comparison of texts would fail on those alone.

The reader is `html.parser` of the standard library. It needs no package of
a service and no package of the template language.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

#: Elements that hold nothing and have no end tag (the HTML standard, §13.1.2).
_VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)

#: What a browser posts for a checked box that names no value.
CHECKED_VALUE: Final = "on"

_ROOT: Final = "#document"


@dataclass(slots=True)
class Element:
    """One element: its tag, its attributes and what is inside it."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict[str, str])
    children: list[Element | str] = field(default_factory=list["Element | str"])

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    @property
    def raw_text(self) -> str:
        """Every character inside the element, as the page gives it."""
        parts = [child if isinstance(child, str) else child.raw_text for child in self.children]

        return "".join(parts)

    @property
    def text(self) -> str:
        """The text a person reads: each run of white space is one space."""
        return " ".join(self.raw_text.split())

    def all(self, tag: str, cls: str | None = None) -> list[Element]:
        """Each element below this one with the tag, and with the class when given."""
        found: list[Element] = []

        for child in self.children:
            if isinstance(child, str):
                continue

            if child.tag == tag and (cls is None or cls in child.classes):
                found.append(child)

            found.extend(child.all(tag, cls))

        return found

    def one(self, tag: str, cls: str | None = None) -> Element:
        """The one element with the tag. Raises when the page holds none or two."""
        found = self.all(tag, cls)

        if len(found) != 1:
            wanted = tag if cls is None else f"{tag}.{cls}"

            raise AssertionError(f"the page holds {len(found)} elements {wanted}, not 1")

        return found[0]

    def links(self) -> list[str]:
        """The target of each link below this element, in page order."""
        return [link.attrs["href"] for link in self.all("a") if "href" in link.attrs]


def parse(text: str) -> Element:
    """The tree of one page. The root holds the elements of the top level."""
    reader = _Reader()
    reader.feed(text)
    reader.close()

    return reader.root


def table_rows(table: Element) -> list[dict[str, Element]]:
    """Each body row of a table, as its cells by the text of the column head.

    A row whose cell count is not the head count is left out: it is a row
    that spans the table, for example "no families".
    """
    heads = [head.text for head in table.one("thead").all("th")]
    rows: list[dict[str, Element]] = []

    for row in table.one("tbody").all("tr"):
        cells = row.all("td")

        if len(cells) == len(heads):
            rows.append(dict(zip(heads, cells, strict=True)))

    return rows


def form_values(form: Element) -> dict[str, str]:
    """What a browser posts for a form, before a button adds its own value.

    A control with no name posts nothing. A disabled control posts nothing.
    A box that is not checked posts nothing. A button posts only when a
    person clicks it, so no button is in the answer.
    """
    values: dict[str, str] = {}

    for control in form.all("input"):
        name = control.attrs.get("name")

        if name is None or "disabled" in control.attrs:
            continue

        if control.attrs.get("type") == "checkbox":
            if "checked" in control.attrs:
                values[name] = control.attrs.get("value", CHECKED_VALUE)

            continue

        values[name] = control.attrs.get("value", "")

    for area in form.all("textarea"):
        name = area.attrs.get("name")

        if name is not None and "disabled" not in area.attrs:
            # A browser drops one line end directly after the start tag.
            values[name] = area.raw_text.removeprefix("\n")

    return values


class _Reader(HTMLParser):
    """Builds the tree. An end tag closes the nearest open element of its name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element(_ROOT)
        self._open: list[Element] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(element)

        if tag not in _VOID:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]

                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)
