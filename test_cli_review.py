import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import eversmart
from history import History
from runtime import process_lock


class CliReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.audit = self.root / 'audit'
        run = self.audit / '20260101T120000Z'
        run.mkdir(parents=True)
        (run / 'manifest.json').write_text(json.dumps({'account': 'synthetic-meter'}))
        self.manifest = run / 'manifest.json'
        with History(self.state / 'history.sqlite') as history:
            history.ingest_archive(self.audit)
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))

    def main(self, *args, **kwargs):
        return eversmart.main(['--state-dir', str(self.state), '--out', str(self.audit), *map(str, args)], **kwargs)

    def test_export_cannot_replace_archive_manifest_or_held_lock(self):
        before = self.manifest.read_bytes()
        lock = self.audit / '.collect.lock'
        with process_lock(lock):
            original_inode = lock.stat().st_ino
            for target in (self.manifest, lock, self.audit / 'new.html', self.state / 'new.html'):
                with self.subTest(target=target.name):
                    with self.assertRaises(SystemExit) as exit_:
                        self.main('--shareable', target)
                    self.assertEqual(exit_.exception.code, 2)
            self.assertEqual(lock.stat().st_ino, original_inode)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_export_resolves_parent_symlinks_before_checking_roots(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.audit, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.main('--shareable', alias / 'summary.html')
        self.assertFalse((self.audit / 'summary.html').exists())

    def test_export_honors_explicit_import_and_bill_directory_boundaries(self):
        for flag in ('--import-archive', '--manual-bill-dir'):
            with self.subTest(flag=flag):
                directory = self.root / flag.removeprefix('--')
                directory.mkdir()
                with self.assertRaises(SystemExit):
                    self.main(flag, directory, '--shareable', directory / 'summary.html')
                self.assertEqual(list(directory.iterdir()), [])

    def test_shareable_never_overwrites_a_destination_created_while_rendering(self):
        destination = self.root / 'summary.html'
        def render(data_dir, staging, **kwargs):
            destination.write_text('created elsewhere')
            staging.write_text('redacted summary')
        self.assertEqual(self.main('--shareable', destination, renderer=render), 1)
        self.assertEqual(destination.read_text(), 'created elsewhere')

    def test_shareable_writes_a_new_private_standalone_file(self):
        destination = self.root / 'summary.html'
        self.assertEqual(self.main('--shareable', destination), 0)
        self.assertIn('Aggregates shared', destination.read_text())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_mistyped_account_does_not_clobber_dashboard(self):
        old = self.state / 'dashboard.html'
        old.write_text('last good')
        self.assertEqual(self.main('--render', '--account', 'typo'), 1)
        self.assertEqual(old.read_text(), 'last good')

    def test_offline_command_rejects_collection_flags_without_opt_in(self):
        with patch.object(eversmart, 'EversourceClient', side_effect=AssertionError('client must stay offline')) as client:
            with self.assertRaises(SystemExit) as exit_:
                self.main('--render', '--resolution', 'DAY')
            self.assertEqual(exit_.exception.code, 2)
            client.assert_not_called()

    def test_broad_state_directory_is_rejected_before_writes(self):
        broad = self.root / 'broad'
        broad.mkdir(mode=0o755)
        self.assertEqual(eversmart.main(['--state-dir', str(broad), '--render']), 1)
        self.assertEqual(list(broad.iterdir()), [])

    def test_render_passes_explicit_manual_bill_meter_scope(self):
        observed = []
        def render(data_dir, staging, **kwargs):
            observed.append(kwargs['manual_bill_account'])
            staging.write_text('dashboard')
        result = self.main('--render', '--account', 'synthetic-meter',
                           '--manual-bill-account', 'synthetic-meter', renderer=render)
        self.assertEqual(result, 0)
        self.assertEqual(observed, ['synthetic-meter'])

    def test_archive_import_reports_skipped_invalid_run(self):
        bad = self.audit / '20260102T120000Z'
        bad.mkdir()
        (bad / 'manifest.json').write_text('{bad json')
        with redirect_stdout(io.StringIO()) as log:
            self.assertEqual(self.main('--import-archive', self.audit), 0)
        self.assertEqual(json.loads(log.getvalue())['skipped_invalid_runs'], 1)
