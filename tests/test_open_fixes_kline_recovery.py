"""V12: real interval failure shapes reproduced through the compat boundary."""
from __future__ import annotations

import pytest
import pandas as pd
from types import SimpleNamespace
from open_harness import open_sdk, request_params
from tgwpy.status import DataEmpty, GatewayStatus, envelope_status
from tgwpy import cli

CODE = '159291.SZ'
DAYS = [20261019, 20261020, 20261021, 20261022, 20261023]


def bar(code, day, close=990000):
    return f'{code},102,{day},1000000,1100000,900000,{close},322190100,318968199000'


def run(reply, codes=None, days=None):
    with open_sdk(reply) as (ad, gw):
        market = ad.MarketData(DAYS if days is None else days)
        result = market.query_kline(codes or [CODE], DAYS[0], DAYS[-1], ad.constant.Period.day.value)
        return result, market.last_kline_report, gw.requests


def dates(frame):
    return frame.kline_time.dt.strftime('%Y%m%d').astype(int).tolist()


@pytest.mark.parametrize('truncated', [False, True])
def test_start_empty_or_successful_tail_truncation(truncated):
    def reply(req):
        p = request_params(req)
        first, last = int(p['begin_date']), int(p['end_date'])
        if first != last:
            return ([bar('159291', DAYS[1])], 0) if truncated else (None, DataEmpty())
        return (None, DataEmpty()) if first == DAYS[0] else ([bar('159291', first)], 0)
    out, audit, requests = run(reply)
    assert dates(out[CODE]) == DAYS[1:]
    assert out[CODE].close.tolist() == [0.99] * 4
    assert out[CODE].volume.tolist() == [3221901] * 4
    assert len(requests) == (5 if truncated else 6)
    assert audit['codes'][CODE]['empty_dates'] == [DAYS[0]]
    assert audit['codes'][CODE]['unresolved_dates'] == [DAYS[0]]
    assert not audit['codes'][CODE]['complete']


@pytest.mark.parametrize('status', [DataEmpty(), 0])
def test_all_empty_is_auditable_not_complete(status):
    out, audit, requests = run(lambda req: (None, status))
    assert out == {CODE: None}
    assert len(requests) == 6
    assert audit['codes'][CODE]['empty_dates'] == DAYS
    assert not audit['codes'][CODE]['complete']


def test_single_day_true_error_is_failure_not_empty():
    with open_sdk(lambda req: (None, GatewayStatus(-82, 'Rejected'))) as (ad, gw):
        obj = ad.MarketData([DAYS[0]])
        with pytest.raises(Exception, match='^查询失败$') as error:
            obj.query_kline([CODE], DAYS[0], DAYS[0], ad.constant.Period.day.value)
        assert len(gw.requests) == 1
        assert error.value.coverage['codes'][CODE]['empty_dates'] == []


def test_mixed_success_empty_failure_keeps_partial_and_fails():
    def reply(req):
        p = request_params(req)
        if p['security_code'] == '159292': return None, DataEmpty()
        if p['security_code'] == '159293': return None, GatewayStatus(301010, 'ParameterError')
        return [bar(p['security_code'], DAYS[0])], 0
    with open_sdk(reply) as (ad, gw):
        obj = ad.MarketData([DAYS[0]])
        with pytest.raises(Exception, match='^查询失败$') as error:
            obj.query_kline([CODE, '159292.SZ', '159293.SZ', '159294.SZ'], DAYS[0], DAYS[-1], ad.constant.Period.day.value)
        assert type(error.value) is Exception
        assert dates(error.value.partial_results[CODE]) == [DAYS[0]]
        assert dates(error.value.partial_results['159294.SZ']) == [DAYS[0]]
        assert error.value.partial_results['159292.SZ'] is None
        assert len(gw.requests) == 4


def test_sort_deduplicate_and_no_redundant_requests():
    out, audit, requests = run(lambda req: ([bar('159291', d) for d in reversed(DAYS)] + [bar('159291', DAYS[0])], 0), codes=[CODE, CODE], days=DAYS[::-1] + [DAYS[0]])
    assert dates(out[CODE]) == DAYS
    assert audit['codes'][CODE]['complete']
    assert len(requests) == 1


def test_conflicting_duplicate_bars_fail():
    with pytest.raises(Exception, match='^查询失败$'):
        run(lambda req: ([bar('159291', DAYS[0]), bar('159291', DAYS[0], 999000)], 0))


def test_budget_is_shared_across_codes_and_fail_closed(monkeypatch):
    monkeypatch.setenv('TGW_KLINE_MAX_RECOVERY_REQUESTS', '3')
    with open_sdk(lambda req: (None, DataEmpty())) as (ad, gw):
        obj = ad.MarketData(DAYS[:2])
        with pytest.raises(Exception, match='^查询失败$') as error:
            obj.query_kline([CODE, '159292.SZ', '159293.SZ'], DAYS[0], DAYS[-1], ad.constant.Period.day.value)
        assert len(gw.requests) == 5  # two initial windows + three daily checks
        assert error.value.coverage['recovery_requests'] == 3
        assert error.value.coverage['not_queried_codes'] == ['159293.SZ']


def test_retry_budget_and_success_after_network_failure(monkeypatch):
    replies = iter([(None, 'network timeout'), ([bar('159291', DAYS[0])], 0)])
    out, audit, requests = run(lambda req: next(replies), days=DAYS[:1])
    assert dates(out[CODE]) == DAYS[:1]
    assert audit['recovery_requests'] == 1
    monkeypatch.setenv('TGW_KLINE_MAX_RECOVERY_REQUESTS', '2')
    with open_sdk(lambda req: (None, 'network timeout')) as (ad, gw):
        with pytest.raises(Exception, match='^查询失败$'):
            ad.MarketData(DAYS).query_kline([CODE], DAYS[0], DAYS[-1], ad.constant.Period.day.value)
        assert len(gw.requests) == 3


@pytest.mark.parametrize('mode', ['qfq', 'hfq'])
def test_recovered_bars_still_require_factors(monkeypatch, mode):
    def reply(req):
        p = request_params(req)
        if p['begin_date'] != p['end_date']: return None, DataEmpty()
        return [bar('159291', int(p['begin_date']))], 0
    out, _, _ = run(reply)
    factors = pd.DataFrame({CODE: [2.] * 4}, index=pd.to_datetime([str(d) for d in DAYS[1:]]))
    monkeypatch.setattr(cli, '_factors_for', lambda *a: {CODE: factors[CODE]})
    ad = SimpleNamespace(BaseData=lambda: None)
    with pytest.raises(cli.AdjustIncomplete): cli.apply_adjust(ad, out, mode)
    adjusted = cli.apply_adjust(ad, out, mode, allow_partial=True)
    assert dates(adjusted[CODE]) == DAYS[1:]
    assert adjusted[CODE].close.tolist() == ([0.99] * 4 if mode == 'qfq' else [1.98] * 4)
    assert len(out[CODE]) == 5  # no in-place raw/adjusted mixing


@pytest.mark.parametrize('env,empty', [
    ({'status': -100, 'headers': {'tag': 'DataEmpty'}}, True),
    ({'status': -100, 'headers': {'tag': 'Error'}}, False),
    ({'status': -100}, False),
    ({'status': -1000, 'headers': {'tag': 'DataEmpty'}}, False),
    ({'status': -82, 'headers': {'tag': 'DataEmpty'}}, False),
    ({'status': -100, 'headers': {'tag': 'DataEmpty'}, 'data': ['row']}, False),
])
def test_typed_empty_requires_matching_envelope(env, empty):
    assert isinstance(envelope_status(env), DataEmpty) == empty
    assert not isinstance(envelope_status(env, partial=True), DataEmpty)


def test_untyped_empty_text_cannot_end_information_pagination():
    from tgwpy.compat.tgw_module import _is_empty_status
    assert not _is_empty_status('unknown error -1000 DataEmpty')
    assert not _is_empty_status('status=-100')
    assert _is_empty_status(DataEmpty())


@pytest.mark.parametrize('row', [bar('159291', 20261026), bar('999999', DAYS[0])])
def test_wrong_date_or_security_is_rejected(row):
    with pytest.raises(Exception, match='^查询失败$'):
        run(lambda req: ([row], 0), days=DAYS[:1])


def test_callback_timeout_stops_without_another_request(monkeypatch):
    from OpenAmazingData import MarketData
    from tgwpy.compat import tgw_module as tgw
    from OpenAmazingData.constant import Period
    calls = []
    monkeypatch.setattr(tgw, 'MARKET_TIMEOUT', .001)
    monkeypatch.setattr(tgw, 'QueryKline', lambda req, cb: calls.append(req))
    obj = MarketData(DAYS)
    with pytest.raises(Exception, match='^查询失败$') as error:
        obj.query_kline([CODE, '159292.SZ'], DAYS[0], DAYS[-1], Period.day.value)
    assert len(calls) == 1
    assert error.value.coverage['not_queried_codes'] == ['159292.SZ']


def test_gate_stop_preserves_exit_five(monkeypatch, capsys):
    from tgwpy.guard import RequestGate, ServerUnavailable
    from tgwpy.compat import tgw_module as tgw
    gate = RequestGate(max_failures=1)
    with open_sdk(lambda req: (None, GatewayStatus(-82, 'Rejected'))) as (ad, gw):
        gw.gate = gate
        gate.record_failure('prior failure')
        market = ad.MarketData(DAYS[:1])
        with pytest.raises(ServerUnavailable):
            market.query_kline([CODE], DAYS[0], DAYS[0], ad.constant.Period.day.value)
    monkeypatch.setattr(cli, 'load_amazingdata', lambda: None)
    monkeypatch.setattr(cli, 'release_session', lambda: None)
    def dispatch(*args): raise ServerUnavailable('gate stopped')
    monkeypatch.setattr(cli, 'dispatch', dispatch)
    assert cli.main(['market', 'kline', '--codes', CODE, '--begin', str(DAYS[0]), '--end', str(DAYS[0])]) == 5
    assert capsys.readouterr().out == ''


def test_cli_failure_never_serializes_partial_results(monkeypatch, capsys):
    error = Exception('查询失败')
    error.partial_results = {CODE: 'unadjusted rows'}
    def fail(*args): raise error
    monkeypatch.setattr(cli, 'load_amazingdata', lambda: None)
    monkeypatch.setattr(cli, 'release_session', lambda: None)
    monkeypatch.setattr(cli, 'dispatch', fail)
    assert cli.main(['market', 'kline', '--codes', CODE, '--begin', str(DAYS[0]), '--end', str(DAYS[0])]) == 3
    assert capsys.readouterr().out == ''
