"""Synthetic OpenDART pages: no real disclosures or network access."""

import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from momentum.config import SpecUndefinedError
from momentum.krx.dart_events import DisclosureLedger, parse_list_pages

PAGE = Path(__file__).parents[1] / 'fixtures/dart/page-1.json'
KST = ZoneInfo('Asia/Seoul')


def test_parse_fields_classification_and_corrections():
    events = parse_list_pages([PAGE])
    assert [e.report_kind for e in events] == [
        'BONUS_AND_RIGHTS', 'BONUS_ISSUE', 'RIGHTS_ISSUE',
        'CAPITAL_REDUCTION', 'STOCK_SPLIT', 'REVERSE_SPLIT', 'OTHER',
    ]
    assert [e.is_correction for e in events] == [False, False, True, False, False, False, True]
    e = events[0]
    assert (e.rcept_no, e.rcept_date, e.corp_code, e.corp_cls, e.stock_code) == (
        '20240603000001', date(2024, 6, 3), '00000001', 'K', '000001',
    )
    assert (e.corp_name, e.report_nm, e.remarks) == ('가상회사1', '유무상증자결정', '')
    assert e.available_at == datetime(2024, 6, 3, 18, tzinfo=KST)
    assert e.available_at.tzinfo == KST
    assert e.source_file_sha256 == hashlib.sha256(PAGE.read_bytes()).hexdigest()
    assert events[-1].stock_code is None
    with pytest.raises(FrozenInstanceError):
        e.corp_cls = 'Y'


@pytest.mark.parametrize(('field', 'value'), [
    ('rcept_no', '20240604000001'), ('rcept_no', '123'),
    ('rcept_dt', '20240230'), ('rcept_dt', '202463'),
    ('corp_cls', 'X'), ('corp_code', '123'), ('stock_code', '12345'),
    ('stock_code', '0030r0'), ('stock_code', '0030R00'), ('stock_code', '0030-0'),
    ('corp_code', None), ('stock_code', None),
])
def test_invalid_fields_rejected(tmp_path, field, value):
    page = json.loads(PAGE.read_text())
    page['list'][0][field] = value
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(page))
    with pytest.raises(SpecUndefinedError, match='invalid.json'):
        parse_list_pages([path])


def test_alphanumeric_krx_code_is_kept(tmp_path):
    # Real 2025 lists carry new listings such as 0030R0 (대신밸류리츠).
    page = json.loads(PAGE.read_text())
    page['list'][0]['stock_code'] = '0030R0'
    path = tmp_path / 'alnum.json'
    path.write_text(json.dumps(page))
    assert parse_list_pages([path])[0].stock_code == '0030R0'


@pytest.mark.parametrize('payload', [
    {'status': '010'}, {'status': '000'}, {'status': '000', 'list': {}}, [],
])
def test_bad_pages_rejected(tmp_path, payload):
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload))
    with pytest.raises(SpecUndefinedError, match='bad.json'):
        parse_list_pages([path])


def test_no_data_page(tmp_path):
    path = tmp_path / 'empty.json'
    path.write_text('{"status":"013","message":"조회된 데이터가 없습니다."}')
    assert parse_list_pages([path]) == []


def test_ledger_idempotence_order_and_information_clock(tmp_path):
    events = parse_list_pages([PAGE])
    path = tmp_path / 'nested/events.jsonl'
    ledger = DisclosureLedger(path)
    result = ledger.append(list(reversed(events)))
    assert (result.added, result.unchanged) == (7, 0)
    original = path.read_bytes()
    result = DisclosureLedger(path).append(events)
    assert (result.added, result.unchanged) == (0, 7)
    assert path.read_bytes() == original
    before = datetime(2024, 6, 3, 17, 59, tzinfo=KST)
    assert ledger.as_of(before) == []
    assert ledger.as_of(before + timedelta(minutes=1)) == list(reversed(events))
    assert ledger.as_of(datetime(2024, 6, 3, 9, tzinfo=UTC)) == list(reversed(events))
    assert ledger.listed_only() == list(reversed(events[:-1]))
    for line in path.read_text().splitlines():
        assert line == json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with pytest.raises(SpecUndefinedError):
        ledger.as_of(before.replace(tzinfo=None))


def test_changed_content_rejected_atomically(tmp_path):
    events = parse_list_pages([PAGE])
    path = tmp_path / 'events.jsonl'
    ledger = DisclosureLedger(path)
    ledger.append(events[:1])
    original = path.read_bytes()
    with pytest.raises(SpecUndefinedError, match='Disclosure changed after recording: 20240603000001'):
        ledger.append([events[1], replace(events[0], report_nm='변경')])
    assert path.read_bytes() == original
    result = ledger.append([replace(events[0], source_file_sha256='a'*64,
                                    available_at=events[0].available_at + timedelta(hours=1))])
    assert (result.added, result.unchanged) == (0, 1)
    assert path.read_bytes() == original


def test_duplicate_conflict_in_single_batch_leaves_no_file(tmp_path):
    event = parse_list_pages([PAGE])[0]
    path = tmp_path / 'events.jsonl'
    with pytest.raises(SpecUndefinedError, match='Disclosure changed after recording'):
        DisclosureLedger(path).append([event, replace(event, remarks='정')])
    assert not path.exists()


def test_cli_summary(tmp_path):
    command = [sys.executable, 'tools/dart_ledger.py', '--pages', str(PAGE),
               '--ledger', str(tmp_path / 'events.jsonl')]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    assert first.stdout.splitlines()[-1] == 'dart-ledger: 7 added, 0 unchanged, 7 total'
    assert 'BONUS_AND_RIGHTS: 1' in first.stdout
    second = subprocess.run(command, capture_output=True, text=True, check=True)
    assert second.stdout.splitlines()[-1] == 'dart-ledger: 0 added, 7 unchanged, 7 total'


@pytest.mark.parametrize(('rcept_no', 'accepted'), [
    ('20240601000001', True),   # numbered two days before rcept_dt (real 2024 pattern)
    ('20240526000001', False),  # more than a week apart
])
def test_reception_number_dated_before_list_date(tmp_path, rcept_no, accepted):
    page = json.loads(PAGE.read_text())
    page['list'] = [page['list'][0]]
    page['list'][0]['rcept_no'] = rcept_no
    path = tmp_path / 'late.json'
    path.write_text(json.dumps(page))
    if not accepted:
        with pytest.raises(SpecUndefinedError, match='rcept_no date differs'):
            parse_list_pages([path])
        return
    (event,) = parse_list_pages([path])
    # Availability follows the later list date, never the earlier number.
    assert event.rcept_date == date(2024, 6, 3)
    assert event.available_at.date() == date(2024, 6, 3)
