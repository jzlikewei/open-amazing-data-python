import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from OpenAmazingData import market, _gateway, base, _session, _diagnostics
from tgwpy.compat import tgw_module as tgw


def records(stream):
    return [json.loads(line.split(' ', 1)[1]) for line in stream.getvalue().splitlines() if line.startswith('[OpenAmazingData] ')]


class FailureDiagnosticsTests(unittest.TestCase):
    def test_market_exhaustion_records_each_attempt_and_keeps_exception_contract(self):
        req = SimpleNamespace(security_code='159519', market_type=101, begin_date=20261013, end_date=20261019)
        def QueryKline(req, callback): callback(None, 'upstream status=-89')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(Exception, '^查询失败$') as caught:
                market._send_batch([req], QueryKline, lambda *args: None)
        self.assertIs(type(caught.exception), Exception)
        logs = records(err)
        self.assertEqual([r['attempt'] for r in logs], [1,2,3,4])
        self.assertTrue(all(r['status']=='upstream status=-89' and r['max_attempts']==4 for r in logs))
        self.assertEqual(logs[-1]['security_code'], '159519')
        self.assertEqual(logs[-1]['begin_date'], 20261013)

    def test_recovery_logs_only_failed_attempt(self):
        req = SimpleNamespace(security_code='510050')
        calls=[];kept=[]
        def QueryKline(req, callback):
            calls.append(req)
            callback(None, 'timeout') if len(calls)==1 else callback('bars',0)
        err=io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            market._send_batch([req], QueryKline, lambda req,data:kept.append(data))
        self.assertEqual(kept,['bars']);self.assertEqual(len(records(err)),1)

    def test_success_and_empty_are_not_failures(self):
        err=io.StringIO()
        with contextlib.redirect_stderr(err):
            for status in [0, tgw.ErrorCode.kDataEmpty]:
                market._send_batch([SimpleNamespace(security_code='159291')],lambda r,cb:cb(None,status),lambda *args:None)
        self.assertEqual(err.getvalue(),'')

    def test_info_records_task_function_and_attempt(self):
        err=io.StringIO()
        with patch.object(tgw,'QueryThirdInfo',side_effect=lambda task_id,query_spi:query_spi(None,'status=-89')),contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(Exception,'^查询失败$'):
                _gateway.fetch_each(['510050.SH'],lambda key:[('function_id','A010050002'),('code',key)],50,retry_times=1)
        logs=records(err)
        self.assertEqual([r['attempt'] for r in logs],[1,2])
        self.assertEqual(logs[0]['function_id'],'A010050002')
        self.assertEqual(logs[0]['code'],'510050.SH')
        self.assertIsInstance(logs[0]['task_id'],int)
        self.assertEqual(logs[0]['max_attempts'],2)

    def test_factor_failure_has_code_and_status(self):
        err=io.StringIO()
        with patch.object(tgw,'QueryExFactorTable',side_effect=lambda code,query_spi:query_spi(None,'status=-89')),contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(Exception,'^查询失败$'):base._ex_factors(['510050.SH'])
        logs=records(err)
        self.assertEqual([r['attempt'] for r in logs],[1,2,3,4])
        self.assertEqual(logs[0]['code'],'510050.SH')

    def test_session_error_redacts_credentials_and_preserves_limit_detection(self):
        err=io.StringIO();out=io.StringIO();watcher=_session._LogWatcher()
        with patch.object(_diagnostics,'_credentials',set()),patch.dict('os.environ',{'TGW_PASSWORD':'env-secret'}),patch.object(tgw.session(),'token','session-secret'):
            _diagnostics.remember_credentials('direct-user','direct secret')
            message='Connections of this user exceed the max limitation; direct-user direct secret env-secret session-secret token="other secret" password=abc Bearer xyz https://u:p@example.org'
            with contextlib.redirect_stderr(err),contextlib.redirect_stdout(out):watcher.OnLog(4,message,len(message))
        self.assertTrue(watcher.max_limitation);self.assertEqual(out.getvalue(),'')
        log=records(err)[0];self.assertEqual(log['event'],'sdk_error')
        for secret in ['direct-user','direct secret','env-secret','session-secret','other secret','abc','xyz','u:p']:
            self.assertNotIn(secret,err.getvalue())
        self.assertIn('[REDACTED]',log['message'])

    def test_broken_log_sink_cannot_break_query_callback(self):
        class Broken(io.StringIO):
            def write(self, text):raise OSError('closed')
        with contextlib.redirect_stderr(Broken()):
            failed=market._send_round([SimpleNamespace(security_code='510050')],lambda r,cb:cb(None,'status=-89'),lambda *args:None)
        self.assertEqual(len(failed),1)

if __name__=='__main__':unittest.main()
