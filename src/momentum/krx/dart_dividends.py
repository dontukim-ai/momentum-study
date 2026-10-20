"""Cash-dividend decisions read from OpenDART filing documents (FIN-02d).

There is no structured API for 현금ㆍ현물배당결정 (I-type exchange filings);
``document.xml`` returns a zip holding one XForms HTML page. Its bytes are
UTF-8 although a ``<meta>`` claims euc-kr, and some tags are never closed,
so the page is read as HTML with the standard-library parser. There are no
field codes: label cells are plain ``<span>`` and value cells are
``<span class="xforms_input">``, so fields are matched by label text, which
varies between form versions (for example item 8 is either 주주총회
개최여부 or 승인기관). Unknown values are refused, never guessed.

A subsidiary filing (지주사의 '자회사의 주요경영사항') names the paying
company only as text in a separate header table; its per-share amount
belongs to that company's shares, not to the filer's stock code, so such
decisions are marked and never attached to the filer.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

from momentum.config import SpecUndefinedError

MAIN_TABLE = "XFormD1_Form0_Table0"
SUBSIDIARY_TABLE = "XFormD2_Form0_Table0"
CORRECTION_TABLE = "XFormD8_Form0_Table1"
_SUBROWS = {"보통주식", "종류주식", "참석(명)", "불참(명)"}


@dataclass(frozen=True)
class DividendDecision:
    rcept_no: str
    dividend_kind: str  # 결산배당, 분기배당, 중간배당 ...
    payment_kind: str  # 현금배당, 현물배당 ...
    per_share_common: Decimal | None
    per_share_class: Decimal | None
    record_date: date
    pay_date: date | None
    board_date: date | None
    agm_date: date | None  # 주주총회 예정일자; absent on some form versions
    subsidiary: str | None  # paying company named in a subsidiary filing
    correction: bool


class _Tables(HTMLParser):
    """Rows of every table with an id: list of (text, is_value) cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[tuple[str, bool]]]] = {}
        self._table: str | None = None
        self._row: list[tuple[str, bool]] | None = None
        self._cell: list[str] | None = None
        self._value = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        table_id = values.get("id")
        if tag == "table" and table_id:
            self._table = table_id
            self.tables[table_id] = []
        elif tag == "tr" and self._table:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell, self._value = [], False
        elif tag == "span" and self._cell is not None:
            if "xforms_input" in (values.get("class") or "").split():
                self._value = True
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell is not None and self._row is not None:
            text = re.sub(r"[ \t\r\f\v\xa0]+", " ", "".join(self._cell)).strip()
            self._row.append((text, self._value))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table:
            if self._row:
                self.tables[self._table].append(self._row)
            self._row = None
        elif tag == "table":
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _fields(rows: list[list[tuple[str, bool]]]) -> dict[tuple[str, str | None], str]:
    fields: dict[tuple[str, str | None], str] = {}
    current: str | None = None
    for row in rows:
        labels = [text for text, is_value in row if not is_value]
        values = [text for text, is_value in row if is_value]
        if not values:
            continue
        if len(labels) >= 2:
            current = labels[0]
            key: tuple[str, str | None] = (labels[0], labels[1])
        elif len(labels) == 1 and labels[0] in _SUBROWS and current:
            key = (current, labels[0])
        elif len(labels) == 1:
            current = labels[0]
            key = (labels[0], None)
        else:
            continue
        fields[key] = values[0]
    return fields


def _find(
    fields: dict[tuple[str, str | None], str], label: str, sub: str | None = None
) -> str:
    matches = [v for (main, part), v in fields.items() if label in main and part == sub]
    if len(matches) != 1:
        raise SpecUndefinedError(
            f"dividend field {label!r}/{sub!r}: {len(matches)} matches"
        )
    return matches[0]


def _optional_day(fields: dict[tuple[str, str | None], str], label: str) -> date | None:
    present = [k for k in fields if label in k[0] and k[1] is None]
    return _day(_find(fields, label)) if present else None


def _amount(text: str) -> Decimal | None:
    if text == "-":
        return None
    if re.fullmatch(r"\d{1,3}(,\d{3})*(\.\d+)?|\d+(\.\d+)?", text) is None:
        raise SpecUndefinedError(f"unreadable amount: {text!r}")
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - guarded by the pattern
        raise SpecUndefinedError(f"unreadable amount: {text!r}") from exc


def _day(text: str) -> date | None:
    if text == "-":
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match is None:
        raise SpecUndefinedError(f"unreadable date: {text!r}")
    return date(*(int(part) for part in match.groups()))


def read_document(raw: bytes) -> str:
    """Page text from a document.xml response (zip) or an extracted page."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
            if len(names) != 1:
                raise SpecUndefinedError(
                    f"expected one document in zip, found {len(names)}"
                )
            raw = archive.read(names[0])
    # The <meta> charset says euc-kr; the bytes are UTF-8 (2026-10-20 check).
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecUndefinedError("document is not UTF-8") from exc


def parse_dividend_document(rcept_no: str, text: str) -> DividendDecision:
    parser = _Tables()
    parser.feed(text)
    parser.close()
    if MAIN_TABLE not in parser.tables:
        raise SpecUndefinedError(f"{rcept_no}: dividend table not found")
    fields = _fields(parser.tables[MAIN_TABLE])
    record = _day(_find(fields, "배당기준일"))
    if record is None:
        raise SpecUndefinedError(f"{rcept_no}: record date not stated")
    subsidiary = None
    if SUBSIDIARY_TABLE in parser.tables:
        names = [t for row in parser.tables[SUBSIDIARY_TABLE] for t, v in row if v]
        if len(names) != 1 or not names[0]:
            raise SpecUndefinedError(f"{rcept_no}: subsidiary name not readable")
        subsidiary = names[0]
    return DividendDecision(
        rcept_no=rcept_no,
        dividend_kind=_find(fields, "배당구분"),
        payment_kind=_find(fields, "배당종류"),
        per_share_common=_amount(_find(fields, "1주당 배당금", "보통주식")),
        per_share_class=_amount(_find(fields, "1주당 배당금", "종류주식")),
        record_date=record,
        pay_date=_day(_find(fields, "배당금지급 예정일자")),
        board_date=_day(_find(fields, "이사회결의일")),
        agm_date=_optional_day(fields, "주주총회 예정일자"),
        subsidiary=subsidiary,
        correction=CORRECTION_TABLE in parser.tables,
    )


def ex_dividend_date(
    record_date: date, sessions: tuple[date, ...], confirmed_through: date
) -> date:
    """Trading day before the last session on or before the record date.

    With T+2 settlement a buyer on the session before the record date's
    session settles too late. Checked against KT&G 2023: record date
    2023-12-31 (non-trading), last session 12-28, observed ex-date 12-27.
    The observed calendar must be confirmed through the record date, else
    a session could be missing and the answer is unknown.
    """
    if confirmed_through < record_date:
        raise SpecUndefinedError(
            f"calendar not confirmed through record date {record_date}"
        )
    on_or_before = sorted(d for d in sessions if d <= record_date)
    if len(on_or_before) < 2:
        raise SpecUndefinedError(
            f"calendar starts too late for record date {record_date}"
        )
    return on_or_before[-2]


def load_documents(paths: list[Path]) -> list[DividendDecision]:
    decisions = []
    for path in paths:
        rcept_no = path.name.split(".")[0]
        if re.fullmatch(r"\d{14}", rcept_no) is None:
            raise SpecUndefinedError(f"{path}: file name must be the 14-digit rcept_no")
        decisions.append(
            parse_dividend_document(rcept_no, read_document(path.read_bytes()))
        )
    return decisions
