import gzip
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import eversmart as app


START = '2026-01-01T00:00:00Z'
END = '2026-03-15T00:00:00Z'
AVAILABLE = f'{START}/{END}'
SECRET = 'synthetic-upstream-secret'
EMPTY = {'data': {'billingAccountByAuthContext': {'serviceAgreementsConnection': {'edges': []}}}}


def usage_doc(target, interval, resolution, value=1.25):
    start, stop = map(datetime.fromisoformat, interval.split('/'))
    length = {'QUARTER_HOUR': 15, 'HALF_HOUR': 30, 'HOUR': 60, 'DAY': 1440}[resolution]
    end = min(stop, start + timedelta(minutes=length))
    point = {'uuid': target.service_point_uuid, 'utilityId': target.account, 'readStreams': {'netUsage': [{
        'serviceQuantityIdentifier': 'NET_USAGE', 'unit': 'KWH', 'reads': [
            {'timeInterval': f'{start.isoformat()}/{end.isoformat()}', 'readType': 'ACTUAL',
             'measuredAmount': {'value': value}},
        ],
    }]}}
    agreement = {'node': {'uuid': target.service_agreement_uuid, 'servicePointsConnection': {'edges': [{'node': point}]}}}
    return {'data': {'billingAccountByAuthContext': {'serviceAgreementsConnection': {'edges': [agreement]}}}}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.targets = [app.Target('meter', 'entity', 'sa', 'sp', AVAILABLE, 'HOUR', 'UTC')]

    def metadata(self):
        return EMPTY

    def service_points(self, metadata):
        return self.targets

    def usage(self, target, interval, resolution):
        self.calls.append((target.account, interval, resolution))
        return usage_doc(target, interval, resolution)

    def cost(self, *args):
        return EMPTY

    pricing = demand_maxima = weather = bills = forecast = rate_static_content = cost


class FakeHistory:
    def __init__(self):
        self.ingested = []
        self.lookups = []

    def coverage(self, account, resolution):
        self.lookups.append((account, resolution))
        return []

    def ingest_run(self, run):
        assert (run / 'manifest.json').is_file()
        self.ingested.append(run)
        return True


class CollectionReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {'EVERSMART_STATE_DIR': str(self.root)}, clear=True))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No outbound traffic in tests')))
        self.out = self.root / 'runs'
        self.output = self.root / 'live.html'
        self.db = self.root / 'test.sqlite3'
        self.client = FakeClient()
        self.history = FakeHistory()
        self.renders = []

    def renderer(self, data_dir, output, *, db_path, account, shareable, split_data):
        self.assertEqual(data_dir, self.out)
        self.assertEqual(db_path, self.db)
        self.assertEqual(account, 'meter')
        self.assertFalse(shareable)
        self.assertFalse(split_data)
        self.assertTrue(self.history.ingested)
        self.renders.append(output)
        output.write_text('fresh')

    def collect(self, interval=None, *, account=None, **kwargs):
        return app.collect_once(self.client, self.out, account, interval, None, history=self.history,
                                db_path=self.db, output=self.output, renderer=self.renderer, **kwargs)

    def test_explicit_interval_is_chunked_end_to_end_and_audited(self):
        result = self.collect(AVAILABLE)
        windows = [interval for _, interval, _ in self.client.calls]
        self.assertEqual(len(windows), 3)
        bounds = [tuple(map(datetime.fromisoformat, window.split('/'))) for window in windows]
        self.assertEqual(bounds[0][0], datetime.fromisoformat(START))
        self.assertEqual(bounds[-1][1], datetime.fromisoformat(END))
        for start, end in bounds:
            self.assertLessEqual(end - start, timedelta(hours=720))
        self.assertEqual([end for _, end in bounds[:-1]], [start for start, _ in bounds[1:]])
        run = self.history.ingested[0]
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        self.assertEqual(len(list(run.glob('usage_*.json.gz'))), 3)
        for raw, window in zip(sorted(run.glob('usage_*.json.gz')), windows, strict=True):
            with gzip.open(raw, 'rt') as stream:
                self.assertEqual(json.load(stream), usage_doc(self.client.targets[0], window, 'HOUR'))
        self.assertTrue((run / 'usage.csv').exists())
        self.assertEqual(result['collection_windows']['meter'], windows)
        self.assertEqual(self.output.read_text(), 'fresh')

    def test_explicit_daily_collection_splits_at_meter_local_midnights(self):
        from zoneinfo import ZoneInfo

        zone = ZoneInfo('America/New_York')
        target = self.client.targets[0]
        target.timezone, target.resolution = zone.key, 'DAY'
        self.collect('2026-02-15T00:00:00-05:00/2026-04-01T00:00:00-04:00')
        self.assertEqual(len(self.client.calls), 2)
        for _, interval, _ in self.client.calls:
            a, b = map(datetime.fromisoformat, interval.split('/'))
            self.assertLessEqual(b - a, timedelta(hours=720))
            for boundary in (a, b):
                self.assertEqual(boundary.astimezone(zone).strftime('%H:%M'), '00:00')

    def test_required_failure_does_not_publish_or_ingest_a_partial_run(self):
        def fail(*args):
            self.assertEqual([p for p in self.out.iterdir() if not p.name.startswith('.')], [])
            raise RuntimeError(SECRET)
        self.client.usage = fail
        self.output.write_text('last good')
        with self.assertRaises(RuntimeError):
            self.collect(AVAILABLE)
        self.assertEqual([p for p in self.out.iterdir() if p.is_dir()], [])
        self.assertEqual(self.history.ingested, [])
        self.assertEqual(self.output.read_text(), 'last good')

    def test_same_audit_directory_cannot_have_overlapping_collectors_with_different_databases(self):
        seen = []

        def usage(target, interval, resolution):
            if not seen:
                seen.append('outside')
                with self.assertRaisesRegex(RuntimeError, 'process lock'):
                    app.collect_once(FakeClient(), self.out, 'meter', interval, resolution,
                                     history=FakeHistory(), db_path=self.root / 'different.sqlite',
                                     output=self.output, renderer=self.renderer)
            return usage_doc(target, interval, resolution)

        self.client.usage = usage
        self.collect(AVAILABLE)
        self.assertEqual(len(list(self.out.glob('*/manifest.json'))), 1)

    def test_optional_failure_publishes_fresh_usage_and_redacts_manifest(self):
        self.output.write_text('last good')
        def fail(*args):
            raise RuntimeError(SECRET)
        self.client.weather = fail
        result = self.collect(AVAILABLE)
        run = self.history.ingested[0]
        self.assertIn('weather_meter', result['errors'])
        self.assertNotIn(SECRET, (run / 'manifest.json').read_text())
        self.assertNotIn(SECRET, (run / 'weather_meter_error.txt').read_text())
        self.assertEqual(len(self.renders), 1)
        self.assertEqual(self.output.read_text(), 'fresh')

    def test_optional_parser_failure_preserves_raw_response(self):
        document = {'data': {'billingAccountByAuthContext': 'synthetic unexpected schema'}}
        self.client.weather = lambda *args: document
        result = self.collect(f'{START}/2026-01-01T01:00:00Z')
        self.assertIn('weather_meter', result['errors'])
        with gzip.open(self.history.ingested[0] / 'weather_meter.json.gz', 'rt') as stream:
            self.assertEqual(json.load(stream), document)

    def test_failed_ingest_keeps_last_dashboard_and_complete_audit(self):
        self.output.write_text('last good')
        def fail(run):
            self.assertTrue((run / 'manifest.json').is_file())
            raise OSError(SECRET)
        self.history.ingest_run = fail
        with self.assertRaises(OSError):
            self.collect(AVAILABLE)
        self.assertEqual(len(list(self.out.glob('*/manifest.json'))), 1)
        self.assertEqual(self.output.read_text(), 'last good')
        self.assertEqual(self.renders, [])

    def test_renderer_can_fail_after_writing_without_replacing_last_dashboard(self):
        self.output.write_text('last good')
        original = self.renderer
        def partial(data_dir, output, **kwargs):
            original(data_dir, output, **kwargs)
            raise ValueError(SECRET)
        self.renderer = partial
        result = self.collect(AVAILABLE)
        self.assertIn('dashboard_error', result)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.output.read_text(), 'last good')

    def test_finalized_manifest_is_not_discoverable_until_directory_publish(self):
        write_json = app.write_json

        def inspect(path, value):
            write_json(path, value)
            if not self.history.ingested and path.name == 'manifest.json':
                self.assertEqual(list(self.out.glob('*/manifest.json')), [])

        with patch.object(app, 'write_json', side_effect=inspect):
            self.collect(AVAILABLE)
        self.assertEqual(len(list(self.out.glob('*/manifest.json'))), 1)

    def test_rejected_ingest_never_invokes_renderer(self):
        self.history.ingest_run = lambda run: False
        with self.assertRaisesRegex(RuntimeError, 'ingest'):
            self.collect(AVAILABLE)
        self.assertEqual(self.renders, [])

    def test_planner_uses_coverage_per_target(self):
        self.client.targets.append(app.Target('second', 'entity', 'sa2', 'sp2', AVAILABLE, 'DAY', 'UTC'))
        self.collect(account='meter', all_service_points=True, backfill=True)
        self.assertEqual(self.history.lookups, [('meter', 'HOUR'), ('second', 'DAY')])
        self.assertEqual({account for account, _, _ in self.client.calls}, {'meter', 'second'})

    def test_unspecified_target_is_rejected_when_ambiguous(self):
        self.client.targets.append(app.Target('second', 'entity', 'sa2', 'sp2', AVAILABLE, 'DAY', 'UTC'))
        with self.assertRaisesRegex(RuntimeError, '--account'):
            self.collect(AVAILABLE)
        self.assertEqual(self.client.calls, [])

    def test_graphql_honors_retry_after(self):
        with patch.dict(os.environ, {'EVERSMART_STATE_DIR': str(self.root)}, clear=True):
            client = app.EversourceClient()
        client.access_token = 'synthetic'
        client.entity_id = 'entity'
        delays = []
        client._sleep = delays.append
        error = urllib.error.HTTPError('https://example.invalid', 429, 'Rate limited', {'Retry-After': '7'}, io.BytesIO(SECRET.encode()))
        with patch.object(client, 'request', side_effect=[error, b'{"data":{"ok":true}}']):
            client.graphql('query { ok }', {})
        self.assertEqual(delays, [7])

    def test_green_button_mutation_is_not_replayed_after_an_ambiguous_failure(self):
        client = app.EversourceClient(state_dir=self.root, entity_id='entity')
        client.access_token = 'synthetic'
        with patch.object(client, '_sleep', create=True), patch.object(client, 'request', side_effect=TimeoutError(SECRET)) as request:
            with self.assertRaises(app.UpstreamError):
                client.generate_usage_export(self.client.targets[0], AVAILABLE)
        request.assert_called_once()

    def test_default_session_paths_are_private_and_bearer_is_not_saved(self):
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root)}, clear=True):
            client = app.EversourceClient()
        self.assertEqual(client.cookie_file.parent, self.root / 'eversmart')
        self.assertEqual(client.cookieinfo.parent, self.root / 'eversmart')
        self.assertEqual(client.config_file.parent, self.root / 'eversmart')
        client._is_token_expired_or_stale = lambda token: False
        client.request = lambda url: b"accessToken: 'synthetic-secret', setEntityIds(['synthetic-entity'])"
        client.load_databrowser()
        self.assertEqual(client.access_token, 'synthetic-secret')
        self.assertEqual(list(self.root.rglob('*')), [])

    def test_shareable_only_does_not_initialize_client_or_publish_live_dashboard(self):
        artifact = self.root / 'export.html'
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        status = state / 'status.json'
        status.write_text('existing live status')
        calls = []
        def renderer(data_dir, output, **kwargs):
            calls.append(kwargs)
            output.write_text('redacted')
        with patch.object(app, 'EversourceClient', side_effect=AssertionError('unexpected login')), redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--state-dir', str(state), '--shareable', str(artifact)], renderer=renderer), 0)
        self.assertEqual(artifact.read_text(), 'redacted')
        self.assertTrue(calls[0]['shareable'])
        self.assertFalse((state / 'dashboard.html').exists())
        self.assertEqual(status.read_text(), 'existing live status')

    def test_cli_render_failure_has_nonzero_sanitized_result(self):
        destination = self.root / 'dashboard.html'
        destination.write_text('last good')
        def renderer(*args, **kwargs):
            raise ValueError(SECRET)
        log = io.StringIO()
        with redirect_stderr(log), redirect_stdout(log):
            self.assertNotEqual(app.main(['--state-dir', str(self.root), '--render'], renderer=renderer), 0)
        self.assertNotIn(SECRET, log.getvalue() + (self.root / 'status.json').read_text())
        self.assertEqual(destination.read_text(), 'last good')

    def test_offline_export_requires_account_when_history_is_ambiguous(self):
        from history import History

        for index, account in enumerate(('meter', 'second'), 1):
            run = self.out / f'2026010{index}T120000Z'
            run.mkdir(parents=True)
            (run / 'manifest.json').write_text(json.dumps({'account': account, 'retrieved_at_utc': run.name}))
        state = self.root / 'state'
        with History(state / 'history.sqlite') as history:
            self.assertEqual(history.ingest_archive(self.out), 2)
        artifact = self.root / 'export.html'
        with redirect_stderr(io.StringIO()) as log:
            self.assertEqual(app.main(['--state-dir', str(state), '--shareable', str(artifact)]), 1)
        self.assertIn('--account', log.getvalue())
        self.assertFalse(artifact.exists())

    def test_poll_lock_is_held_across_sleep_and_retry_after_is_honored(self):
        seen = []
        def collect(*args, **kwargs):
            seen.append('collect')
            if len(seen) == 1:
                raise urllib.error.HTTPError('https://example.invalid', 503, SECRET, {'Retry-After': '27'}, None)
            return {'account': 'meter', 'errors': {}, 'warnings': {}}
        def sleep(delay):
            self.assertEqual(app.main(['--state-dir', str(self.root), '--render']), 1)
            seen.append(delay)
            if len(seen) == 4:
                raise KeyboardInterrupt
        log = io.StringIO()
        with patch.object(app, 'collect_once', side_effect=collect), redirect_stderr(log), redirect_stdout(log):
            code = app.main(['--state-dir', str(self.root), '--poll', '5'], client_factory=lambda **kw: self.client, sleep=sleep)
        self.assertEqual(code, 130)
        self.assertEqual(seen, ['collect', 27, 'collect', 5])
        self.assertNotIn(SECRET, log.getvalue())

    def test_poll_rejects_green_button_before_client_initialization(self):
        with patch.object(app, 'EversourceClient', side_effect=AssertionError('unexpected login')), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                app.main(['--state-dir', str(self.root), '--poll', '60', '--green-button'])
        self.assertEqual(caught.exception.code, 2)

    def test_archive_import_is_lossless_idempotent_and_offline(self):
        from history import History

        archive = self.root / 'archive'
        run = archive / '20260103T120000Z'
        run.mkdir(parents=True)
        (run / 'manifest.json').write_text(json.dumps({
            'account': 'meter', 'resolution': 'HOUR', 'retrieved_at_utc': run.name,
        }))
        (run / 'usage.csv').write_text(
            'utility_id,stream,serviceQuantityIdentifier,unit,timeInterval,readType,value\n'
            'meter,netUsage,NET_USAGE,KWH,2026-01-02T00:00:00Z/2026-01-02T01:00:00Z,ACTUAL,1.25\n'
        )
        before = {path.name: path.read_bytes() for path in run.iterdir()}
        state = self.root / 'state'
        args = ['--state-dir', str(state), '--import-archive', str(archive)]
        with patch.object(app, 'EversourceClient', side_effect=AssertionError('unexpected login')):
            for expected in (1, 0):
                with redirect_stdout(io.StringIO()) as log:
                    self.assertEqual(app.main(args), 0)
                self.assertEqual(json.loads(log.getvalue())['imported_runs'], expected)
        with History(state / 'history.sqlite') as history:
            self.assertEqual(len(history.coverage('meter', 'HOUR')), 1)
        self.assertEqual({path.name: path.read_bytes() for path in run.iterdir()}, before)
        self.assertFalse((state / 'dashboard.html').exists())

    def test_same_second_runs_are_all_importable(self):
        from history import History

        instant = datetime(2026, 5, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with patch.object(app, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = instant
            for _ in range(2):
                self.collect(AVAILABLE)
        self.assertEqual(len(list(self.out.glob('*/manifest.json'))), 2)
        with History(self.db) as history:
            self.assertEqual(history.ingest_archive(self.out), 2)

    def test_required_provider_error_document_cannot_publish_dashboard(self):
        self.client.usage = lambda *args: {'errors': [{'message': SECRET}]}
        with self.assertRaises(RuntimeError):
            self.collect(AVAILABLE)
        self.assertEqual(self.history.ingested, [])
        self.assertFalse(self.output.exists())

    def test_cli_collects_to_real_history_and_renders_from_ingested_reads(self):
        from history import History

        interval = f'{START}/2026-01-01T01:00:00Z'
        point = {'uuid': 'sp', 'utilityId': 'meter', 'readStreams': {'netUsage': [{
            'serviceQuantityIdentifier': 'NET_USAGE', 'unit': 'KWH', 'reads': [
                {'timeInterval': interval, 'readType': 'ACTUAL', 'measuredAmount': {'value': 1.25}},
            ],
        }]}}
        agreement = {'node': {'uuid': 'sa', 'servicePointsConnection': {'edges': [{'node': point}]}}}
        self.client.usage = lambda *args: {'data': {'billingAccountByAuthContext': {
            'serviceAgreementsConnection': {'edges': [agreement]},
        }}}
        with redirect_stdout(io.StringIO()):
            code = app.main(['--interval', interval], client_factory=lambda **kw: self.client)
        self.assertEqual(code, 0)
        with History(self.root / 'history.sqlite') as history:
            self.assertEqual(history.rows('usage', 'meter')[0]['value'], '1.25')
        html = (self.root / 'dashboard.html').read_text()
        self.assertIn('"total_net_kwh":1.25', html)
