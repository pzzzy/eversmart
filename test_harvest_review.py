import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import eversmart as app
from history import History


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def window(start, end):
    return f'{stamp(START + timedelta(hours=start))}/{stamp(START + timedelta(hours=end))}'


def envelope(**account):
    return {'data': {'billingAccountByAuthContext': account}}


def stream_document(target, name, reads, **other):
    streams = {name: [{'serviceQuantityIdentifier': 'NET_USAGE', 'unit': 'KWH', 'reads': reads}], **other}
    point = {'uuid': target.service_point_uuid, 'utilityId': target.account, 'readStreams': streams}
    agreement = {'uuid': target.service_agreement_uuid, 'servicePointsConnection': {'edges': [{'node': point}]}}
    return envelope(serviceAgreementsConnection={'edges': [{'node': agreement}]})


class Client:
    def __init__(self):
        self.targets = [app.Target('meter', 'entity', 'agreement', 'point', window(0, 4), 'HOUR', 'UTC',
                                   premise_uuid='premise')]
        self.calls = []
        self.fail = {}
        self.empty = set()
        self.value = 1

    def metadata(self):
        self.calls.append(('metadata', None, None))
        return envelope()

    def service_points(self, metadata):
        return self.targets

    def reads(self, dataset, target, interval):
        self.calls.append((dataset, target.account, interval))
        if error := self.fail.get((dataset, target.account, interval), self.fail.get(dataset)):
            raise error
        if (dataset, target.account) in self.empty:
            return []
        start, end = map(datetime.fromisoformat, interval.split('/'))
        step = timedelta(hours=1)
        reads = []
        while start + step <= end:
            reads.append({'timeInterval': f'{stamp(start)}/{stamp(start + step)}', 'readType': 'ACTUAL',
                          'measuredAmount': {'value': self.value}, 'monetaryAmount': {'value': self.value}})
            start += step
        return reads

    def usage(self, target, interval, resolution):
        return stream_document(target, 'netUsage', self.reads('usage', target, interval))

    def cost(self, target, interval, resolution):
        return stream_document(target, 'netUsage', self.reads('cost', target, interval))

    def pricing(self, target, interval, resolution):
        return stream_document(target, 'netUsage', self.reads('pricing', target, interval), rates=[])

    def demand_maxima(self, target, interval):
        self.reads('demand_maxima', target, interval)
        return stream_document(target, 'demand', [])

    def weather(self, target, interval):
        reads = self.reads('weather', target, interval)
        return envelope(premisesConnection={'edges': [{'node': {'uuid': target.premise_uuid, 'weather': [
            {'timeInterval': row['timeInterval'], 'meanTemperature': {'value': 61}} for row in reads
        ]}}]})

    def bills(self, target):
        return envelope(bills=[])

    def forecast(self, target):
        return envelope(billForecast=None)

    def rate_static_content(self, target):
        return envelope()


class HarvestReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No network')))
        self.client = Client()
        self.data = self.root / 'runs'
        self.db = self.root / 'history.sqlite'
        self.output = self.root / 'dashboard.html'
        self.renders = []

    def render(self, _, staged, **options):
        self.renders.append(options)
        staged.write_text(f'render {len(self.renders)}')

    def collect(self, interval=None, **options):
        return app.collect_once(self.client, self.data, 'meter', interval, None, db_path=self.db,
                                output=self.output, renderer=self.render, **options)

    def calls(self, dataset):
        return [interval for name, _, interval in self.client.calls if name == dataset]

    def test_wrong_point_or_shell_cannot_finalize(self):
        target = self.client.targets[0]
        wrong_target = app.Target('other', target.entity_id, target.service_agreement_uuid, 'other-point',
                                  target.available_interval, target.resolution, target.timezone)
        wrong_account = app.Target('other', target.entity_id, target.service_agreement_uuid, target.service_point_uuid,
                                   target.available_interval, target.resolution, target.timezone)
        wrong_agreement = app.Target(target.account, target.entity_id, 'other-agreement', target.service_point_uuid,
                                     target.available_interval, target.resolution, target.timezone)
        proper = self.client.usage(target, window(0, 4), 'HOUR')
        foreign = lambda t: stream_document(t, 'netUsage', [{'timeInterval': window(0, 1), 'measuredAmount': {'value': 4}}])
        for doc in [envelope(), envelope(serviceAgreementsConnection={'edges': []}), foreign(wrong_target),
                    foreign(wrong_account), foreign(wrong_agreement), stream_document(target, 'netUsage', [{}]),
                    {**proper, 'errors': [{'message': 'must not be accepted as a partial GraphQL result'}]}]:
            with self.subTest(doc=doc):
                with patch.object(self.client, 'usage', return_value=doc), self.assertRaises(app.UpstreamError):
                    self.collect(window(0, 4))
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.data.glob('*/manifest.json')), [])

    def test_valid_empty_does_not_advance_primary_or_render_empty_dashboard(self):
        self.client.empty.update((name, 'meter') for name in ('usage', 'weather', 'cost', 'pricing'))
        result = self.collect(window(0, 4))
        self.assertEqual(result['poll_status'], 'no_op')
        self.assertFalse(result['success'])
        self.assertFalse(self.output.exists())
        with History(self.db) as store:
            manifest = store.latest_manifest('meter')
            assert manifest is not None
            self.assertEqual(manifest['poll_status'], 'no_op')
            self.assertEqual(store.coverage('meter', 'HOUR'), [])
        (Path(result['output_dir']) / 'usage.csv').unlink()
        with History(self.root / 'recovered.sqlite') as store:
            self.assertEqual(store.ingest_archive(self.data), 0)
            self.assertEqual(store.archive_failures, 1)

    def test_secondary_usage_cannot_advance_primary_success(self):
        first = self.collect(window(0, 4))
        self.client.targets.append(app.Target('second', 'entity', 'agreement', 'point2', window(0, 4), 'HOUR',
                                              'UTC', premise_uuid='premise2'))
        self.client.empty.add(('usage', 'meter'))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(app._supervise(lambda: self.collect(window(0, 4), all_service_points=True),
                                            self.root, None, None), 0)
        with History(self.db) as store:
            latest = store.latest_manifest('meter')
            assert latest is not None
            self.assertEqual(latest['usage_rows'], 4)
            self.assertEqual(latest['primary_usage_rows'], 0)
        status = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(status['status'], 'no_op')
        self.assertEqual(status['last_successful_poll_at_utc'], first['retrieved_at_utc'])

    def test_foreign_reads_are_preserved_raw_but_not_collected_as_primary(self):
        target = self.client.targets[0]
        other = app.Target('other', 'entity', target.service_agreement_uuid, 'point2', window(0, 4), 'HOUR', 'UTC')
        doc = stream_document(target, 'netUsage', [])
        foreign = stream_document(other, 'netUsage', self.client.reads('usage', other, window(0, 4)))
        def edges(response):
            return response['data']['billingAccountByAuthContext']['serviceAgreementsConnection']['edges'][0]['node']['servicePointsConnection']['edges']
        edges(doc).extend(edges(foreign))
        with patch.object(self.client, 'usage', return_value=doc):
            result = self.collect(window(0, 4))
        self.assertEqual(result['usage_rows'], 0)
        raw, = Path(result['output_dir']).glob('usage_*.json.gz')
        with gzip.open(raw, 'rt') as source:
            self.assertEqual(json.load(source), doc)

    def test_upstream_padding_does_not_invalidate_the_requested_usage(self):
        target = self.client.targets[0]
        original = self.client.usage(target, window(0, 4), 'HOUR')
        point = original['data']['billingAccountByAuthContext']['serviceAgreementsConnection']['edges'][0]['node']['servicePointsConnection']['edges'][0]['node']
        point['readStreams']['netUsage'][0]['reads'].append(
            {'timeInterval': window(4, 5), 'readType': 'ACTUAL', 'measuredAmount': {'value': 4}})
        self.client.usage = lambda *args: original
        result = self.collect(window(0, 4))
        self.assertEqual(result['usage_rows'], 4)
        self.assertEqual(result['poll_status'], 'collected')

    def test_stale_readings_do_not_count_as_requested_usage(self):
        target = self.client.targets[0]
        stale = stream_document(target, 'netUsage', [{'timeInterval': window(4, 5), 'measuredAmount': {'value': 4}}])
        with patch.object(self.client, 'usage', return_value=stale), self.assertRaises(app.UpstreamError):
            self.collect(window(0, 4))

    def test_auxiliary_outage_outlives_overlap_and_repairs_without_refetching_usage(self):
        self.client.fail.update(weather=TimeoutError(), pricing=TimeoutError())
        self.collect(overlap_hours=0)
        for stop in (40, 80, 120):
            self.client.targets[0].available_interval = window(0, stop)
            self.collect(overlap_hours=0)
        # Start from a new DB; finalized manifests alone must recover pending work.
        self.db = self.root / 'recovered.sqlite'
        self.client.calls.clear()
        self.client.fail.pop('weather')
        repair = self.collect(backfill=True, overlap_hours=72)
        self.assertEqual(repair['poll_status'], 'no_op')
        self.assertEqual(self.calls('usage'), [])
        self.assertEqual(self.calls('cost'), [])
        self.assertEqual(self.calls('demand_maxima'), [])
        self.assertEqual(self.calls('weather'), [window(0, 120)])
        self.assertEqual(self.calls('pricing'), [window(0, 120)])
        with History(self.db) as store:
            self.assertEqual(len(store.rows('weather', 'meter')), 120)
            self.assertEqual(len(store.rows('cost', 'meter')), 120)
            self.assertEqual(store.rows('pricing', 'meter'), [])
        self.client.calls.clear()
        self.client.fail.clear()
        self.collect(overlap_hours=0)
        self.assertEqual(self.calls('usage'), [])
        self.assertEqual(self.calls('weather'), [])
        self.assertEqual(self.calls('pricing'), [window(0, 120)])

    def test_partial_auxiliary_success_does_not_rerequest_good_windows(self):
        windows = [window(0, 720), window(720, 721)]
        self.client.targets[0].available_interval = window(0, 721)
        self.client.fail['weather', 'meter', windows[1]] = TimeoutError()
        self.collect(window(0, 721))
        with History(self.db) as store:
            self.assertEqual(len(store.rows('weather', 'meter')), 720)
        self.client.calls.clear()
        self.client.fail.clear()
        self.collect(backfill=True)
        self.assertEqual(self.calls('usage'), [])
        self.assertEqual(self.calls('weather'), [windows[1]])
        self.assertEqual(self.calls('cost'), [])
        with History(self.db) as store:
            self.assertEqual(len(store.rows('weather', 'meter')), 721)

    def test_failed_overlap_refresh_only_queues_new_auxiliary_gap(self):
        self.collect(overlap_hours=72)
        self.client.targets[0].available_interval = window(0, 8)
        self.client.fail['weather'] = TimeoutError()
        self.client.calls.clear()
        self.collect(overlap_hours=72)
        self.assertEqual(self.calls('weather'), [window(0, 8)])
        self.client.calls.clear()
        self.client.fail.clear()
        self.collect(overlap_hours=0)
        self.assertEqual(self.calls('usage'), [])
        self.assertEqual(self.calls('weather'), [window(4, 8)])

    def test_structurally_incomplete_auxiliary_payload_remains_pending(self):
        malformed = envelope(premisesConnection={'edges': [
            {'node': {'uuid': 'premise', 'weather': [{}]}}
        ]})
        with patch.object(self.client, 'weather', return_value=malformed):
            first = self.collect(overlap_hours=0)
        self.assertIn('weather_meter', first['errors'])
        self.assertEqual(first['weather_rows'], 0)
        repaired = self.collect(overlap_hours=0)
        self.assertEqual(repaired['weather_rows'], 4)
        self.assertEqual(self.calls('usage'), [window(0, 4)])

    def test_pending_window_is_repaired_only_for_its_service_point(self):
        self.client.targets.append(app.Target('second', 'entity', 'agreement', 'point2', window(0, 4), 'HOUR',
                                              'UTC', premise_uuid='premise2'))
        self.client.fail['pricing', 'second', window(0, 4)] = TimeoutError()
        self.collect(overlap_hours=0, all_service_points=True)
        self.client.calls.clear()
        self.client.fail.clear()
        self.collect(overlap_hours=0)
        self.assertEqual(self.calls('pricing'), [])
        self.client.calls.clear()
        self.collect(overlap_hours=0, all_service_points=True)
        self.assertEqual(self.calls('usage'), [])
        self.assertEqual([call for call in self.client.calls if call[0] == 'pricing'],
                         [('pricing', 'second', window(0, 4))])

    def test_retry_after_defers_skipped_auxiliary_across_invocations(self):
        self.client.fail['cost'] = app.UpstreamError('not for status', status=429, retry_after=3600)
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        with patch.object(app, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            first = self.collect(overlap_hours=0)
            self.assertEqual(first['retry_after_seconds'], 3600)
            self.assertEqual(self.calls('pricing'), [])
            self.client.calls.clear()
            clock.now.return_value = now + timedelta(minutes=5)
            deferred = self.collect(overlap_hours=0)
            self.assertEqual(deferred['poll_status'], 'deferred')
            self.assertEqual(deferred['retry_after_seconds'], 3300)
            self.assertEqual(self.client.calls, [])
            self.client.fail.clear()
            clock.now.return_value = now + timedelta(hours=1)
            self.collect(overlap_hours=0)
        self.assertEqual(self.calls('usage'), [])
        for dataset in ('cost', 'pricing', 'weather', 'demand_maxima'):
            self.assertEqual(self.calls(dataset), [window(0, 4)])

    def test_large_repair_is_bounded_and_progresses_on_later_polls(self):
        stop = 12 * 720
        self.client.fail['weather'] = TimeoutError()
        # The explicit usage request establishes coverage without requiring every
        # historical auxiliary window to succeed on this invocation.
        self.collect(window(0, stop))
        self.client.targets[0].available_interval = window(0, stop)
        self.client.fail.clear()
        self.client.calls.clear()
        self.collect(overlap_hours=0)
        first = self.calls('weather')
        self.assertTrue(0 < len(first) < 12)
        self.assertEqual(self.calls('usage'), [])
        for _ in range(12):
            self.collect(overlap_hours=0)
        self.assertEqual(self.calls('weather'), [window(i, i + 720) for i in range(0, stop, 720)])
        self.assertEqual(self.calls('usage'), [])

    def test_archive_recovery_diagnostic_and_target_timezone(self):
        with patch('history.plan_windows', return_value=[]) as planner, \
                patch.object(History, 'ingest_archive', autospec=True) as recovery:
            def recover(store, _):
                store.archive_failures = 3
                return 0
            recovery.side_effect = recover
            result = self.collect()
        self.assertEqual(planner.call_args.kwargs['timezone'], 'UTC')
        self.assertEqual(result['archive_failures'], 3)
        self.assertIn('archive_recovery', result['warnings'])

    def test_finalized_manifest_is_unchanged_by_dashboard_outcome(self):
        def render(_, staged, **options):
            manifest, = self.data.glob('*/manifest.json')
            before = manifest.read_bytes()
            staged.write_text('published')
            self.renders.append((manifest, before))

        with patch.object(self, 'render', side_effect=render):
            result = self.collect(window(0, 4))
        manifest, before = self.renders[0]
        self.assertEqual(self.output.read_text(), 'published')
        self.assertIn('dashboard', result)
        self.assertEqual(manifest.read_bytes(), before)
        with History(self.db) as store:
            self.assertEqual(json.loads(before), store.latest_manifest('meter'))
        self.assertNotIn('dashboard', json.loads(before))

    def test_status_failure_after_publish_still_acknowledges_success(self):
        writer = app.write_json

        def write(path, value):
            if path.name == 'status.json':
                raise OSError('secret path or output cannot be disclosed')
            writer(path, value)

        with patch.object(app, 'write_json', side_effect=write), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = app._supervise(lambda: self.collect(window(0, 4)), self.root, None, None)
        self.assertEqual(code, 0)
        self.assertTrue(self.output.is_file())
        result = json.loads(out.getvalue())
        self.assertEqual(result['dashboard'], 'published')
        self.assertIn('status_write_error', result)
        self.assertNotIn('secret', out.getvalue() + err.getvalue())

    def test_fixed_clock_revisions_reimport_in_retrieval_order(self):
        now = datetime(2026, 2, 1, 2, 3, 4, 5, timezone.utc)
        with patch.object(app, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            observed = []
            for self.client.value in (1, 10, 2):
                observed.append(self.collect(window(0, 4))['retrieved_at_utc'])
        self.assertEqual(observed, [stamp(now + timedelta(microseconds=i)) for i in range(3)])
        with History(self.root / 'fresh.sqlite') as store:
            self.assertEqual(store.ingest_archive(self.data), 3)
            self.assertEqual({row['value'] for row in store.rows('usage', 'meter')}, {'2'})


if __name__ == '__main__':
    unittest.main()
