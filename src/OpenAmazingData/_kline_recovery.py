"""Bounded daily-bar gap recovery. Empty days remain observable gaps."""
from __future__ import annotations

import os
import threading
import time

import pandas as pd

from tgwpy.compat import tgw_module as tgw
from tgwpy.guard import ServerUnavailable
from tgwpy.status import DataEmpty, GatewayStatus
from ._diagnostics import emit_failure, request_context
from ._gateway import is_failed


class _BudgetExhausted(Exception):
    pass


def _validated(frame, req, days):
    if frame is None or frame.empty:
        return None
    required = {'kline_time', 'open_price', 'high_price', 'low_price', 'close_price', 'volume_trade', 'value_trade'}
    if not required.issubset(frame.columns):
        raise ValueError('daily response missing bar fields')
    frame = frame.copy()
    stamps = pd.to_datetime(frame['kline_time'].astype(str), format='%Y%m%d', errors='raise')
    dates = stamps.dt.strftime('%Y%m%d').astype(int)
    if not dates.isin(days).all():
        raise ValueError('daily response contains dates outside the requested calendar window')
    if 'security_code' in frame and frame.security_code.astype(str).ne(req.security_code).any():
        raise ValueError('daily response contains a different security')
    frame = frame.drop_duplicates()
    if frame.kline_time.duplicated().any():
        raise ValueError('conflicting daily bars for the same date')
    return frame.sort_values('kline_time').reset_index(drop=True)


def query_daily(owner, codes, days, period, begin_time, end_time):
    # Import lazily: market supplies the unchanged request/price conversion code.
    from .market import (_kline_request, _kline_frame, _time_window, KLINE_SPAN,
                         KLINE_WINDOW, RETRY_TIMES)
    from ._codes import split_code

    limit = int(os.environ.get('TGW_KLINE_MAX_RECOVERY_REQUESTS', '64'))
    if limit < 0:
        raise ValueError('TGW_KLINE_MAX_RECOVERY_REQUESTS must be nonnegative')
    started = time.monotonic()
    report = {'requests': 0, 'recovery_requests': 0, 'recovery_limit': limit,
              'codes': {}, 'elapsed_seconds': 0.0}
    owner.last_kline_report = report
    results, errors = {}, {}

    def send(req, *, extra=False):
        if extra:
            if report['recovery_requests'] >= limit:
                raise _BudgetExhausted('daily recovery request budget exhausted')
            report['recovery_requests'] += 1
        report['requests'] += 1
        done = threading.Event()
        reply = []
        def callback(data, status):
            if not done.is_set():
                reply.append((data, status))
                done.set()
        tgw.QueryKline(req, callback)
        if not done.wait(tgw.MARKET_TIMEOUT):
            done.set()
            # Do not submit another query while the original could still be live.
            raise TimeoutError('daily query callback timed out')
        return reply[0]

    def fetch(req, window_days, *, extra=False):
        for attempt in range(RETRY_TIMES + 1):
            data, status = send(req, extra=extra or attempt > 0)
            if isinstance(status, DataEmpty):
                return None, 'DataEmpty'
            if not is_failed(status):
                frame = _validated(data, req, window_days)
                return frame, 'empty_success' if frame is None else 'success'
            emit_failure('query_failed', method='QueryKline', attempt=attempt + 1,
                         max_attempts=RETRY_TIMES + 1, status=status, **request_context(req))
            # Gate and transport remain authoritative for rate limiting/locking.
            transport = tgw.session().transport
            gate = getattr(transport, 'gate', None)
            if gate is not None and gate.failures >= gate.max_failures:
                raise ServerUnavailable(str(status))
            # Known server rejections other than transient -89 cannot be cured
            # by changing dates. Bare strings retain the legacy retry policy.
            if isinstance(status, GatewayStatus) and status.status != -89:
                raise ValueError(str(status))
        raise ValueError(str(status))

    days = sorted(set(int(day) for day in days))
    window = KLINE_WINDOW[period]
    for code in dict.fromkeys(codes):
        coverage = {'expected_dates': days, 'returned_dates': [], 'empty_dates': [],
                    'unresolved_dates': [], 'empty_responses': {}, 'complete': False}
        report['codes'][code] = coverage
        parts = []
        fatal = None
        try:
            market, symbol = split_code(code)
            begin, end = _time_window(begin_time, end_time, market, KLINE_SPAN)
            for start in range(0, len(days), window):
                chunk = days[start:start + window]
                req = _kline_request(market, symbol, period, chunk, begin, end)
                frame, outcome = fetch(req, chunk)
                if frame is not None:
                    parts.append(frame)
                present = set() if frame is None else set(frame.kline_time.astype(int))
                missing = [day for day in chunk if day not in present]
                for day in missing:
                    if len(chunk) == 1:
                        daily, empty_kind = frame, outcome
                    else:
                        req = _kline_request(market, symbol, period, [day], begin, end)
                        daily, empty_kind = fetch(req, [day], extra=True)
                        if daily is not None:
                            parts.append(daily)
                    if daily is None:
                        coverage['empty_dates'].append(day)
                        coverage['empty_responses'][str(day)] = empty_kind
            # No synthetic bars, previous-close fills or whole-window success claims.
        except (ServerUnavailable, TimeoutError, _BudgetExhausted) as exc:
            errors[code] = str(exc)
            fatal = exc
        except Exception as exc:
            errors[code] = str(exc)
        if parts:
            results[code] = _kline_frame(code, period, parts)
            coverage['returned_dates'] = [int(d.strftime('%Y%m%d')) for d in results[code].kline_time]
        else:
            results[code] = None
        coverage['unresolved_dates'] = sorted(set(days) - set(coverage['returned_dates']))
        coverage['complete'] = not coverage['unresolved_dates'] and code not in errors
        if code in errors:
            coverage['error'] = errors[code]
        if coverage['unresolved_dates'] or code in errors:
            emit_failure('kline_coverage_gap', code=code,
                         missing_dates=coverage['unresolved_dates'],
                         empty_dates=coverage['empty_dates'], error=errors.get(code),
                         requests=report['requests'], recovery_requests=report['recovery_requests'])
        if fatal is not None:
            break
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    report['not_queried_codes'] = [c for c in dict.fromkeys(codes) if c not in results]
    if errors:
        # Keep the established exception type/message and CLI nonzero/empty stdout.
        # Direct SDK callers can inspect partial raw bars, never auto-consume them.
        error = fatal if isinstance(fatal, ServerUnavailable) else Exception('查询失败')
        error.partial_results = results
        error.coverage = report
        raise error
    return results
