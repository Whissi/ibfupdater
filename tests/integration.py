#!/usr/bin/env python3
"""Exercise real Bash/curl transfers using only temporary files and localhost."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'ibfupdater'
BASH = os.environ.get('IBFU_TEST_BASH', 'bash')
STAMP = 'Wed, 21 Oct 2015 07:28:00 GMT'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = self.server.state
        state['requests'].append((self.path, dict(self.headers)))
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/file')
            self.send_header('ETag', '"redirect-only"')
            self.end_headers()
            return
        if self.path == '/slow':
            state['ready'].set()
            if not state['release'].wait(8):
                return
        code = state.get('status', 200)
        if code == 200:
            if state.get('etag') and self.headers.get('If-None-Match') == state['etag']:
                code = 304
            elif not self.headers.get('If-None-Match') and state.get('modified') and self.headers.get('If-Modified-Since') == state['modified']:
                code = 304
        self.send_response(code)
        if code != 304 or state.get('headers_on_304', True):
            if state.get('etag'):
                self.send_header('eTaG', state['etag'])
            if state.get('modified'):
                self.send_header('Last-Modified', state['modified'])
        body = state.get('bodies', {}).get(self.path, state.get('body', b'first\n'))
        if state.get('gzip'):
            body = gzip.compress(body)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(body) + (10 if state.get('truncate') else 0)))
        self.end_headers()
        if code != 304:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, *args):
        pass


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ibfu-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'file.dat'
        self.meta = self.root / '.file.dat.ibfu-meta'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        NO_PROXY='*', no_proxy='*', HOME=str(self.root))
        self.state = {'body': b'first\n', 'requests': [], 'ready': threading.Event(),
                      'release': threading.Event()}
        self.server.state = self.state
        self.addCleanup(self.state['release'].set)
        self.url = f'http://127.0.0.1:{self.server.server_port}/file'

    def args(self, *extra, url=None, output=None, verbose=False):
        return [BASH, str(SCRIPT), '--verbose' if verbose else '--quiet', '--output', str(output or self.output),
                *extra, url or self.url]

    def launch(self, *extra, **kwargs):
        child = subprocess.Popen(self.args(*extra, **kwargs), env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        self.addCleanup(self.stop, child)
        return child

    @staticmethod
    def stop(child):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate(timeout=5)

    def finish(self, child, code=0, timeout=12):
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop(child)
            raise
        self.assertEqual(child.returncode, code, (out, err))
        return out, err

    def run_update(self, *extra, code=0, **kwargs):
        return self.finish(self.launch(*extra, **kwargs), code)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/usr/bin/env bash\n' + body + '\n')
        path.chmod(0o755)

    def backup(self, number):
        return Path(str(self.output) + '.' + str(number))

    def test_verbose_explains_download_cache_and_rotation(self):
        self.state.update(etag='"v1"', modified=STAMP)
        out, err = self.run_update('--enable-caching', '--keep', '1', verbose=True)
        self.assertEqual(err, '')
        for message in ('Output lock acquired', 'no metadata file exists',
                        'HTTP status 200', 'initial download', 'No existing output file',
                        'New version successfully installed', 'Cache metadata successfully updated',
                        'Removing working directory'):
            self.assertIn(message, out)
        out, _ = self.run_update('--enable-caching', '--keep', '1', verbose=True)
        for message in ('Using ETag', 'HTTP status 304', 'cached file is up to date'):
            self.assertIn(message, out)
        self.assertNotIn('Publishing downloaded file', out)
        out, _ = self.run_update('--enable-caching', '--force', '--keep', '1', verbose=True)
        self.assertIn('due to ‘--force’', out)
        self.assertIn('Content is unchanged', out)
        self.assertFalse(self.backup(1).exists())
        for version in (2, 3):
            self.state.update(etag=f'"v{version}"', body=f'version {version}'.encode())
            out, _ = self.run_update('--enable-caching', '--keep', '1', verbose=True)
            for message in ('Content has changed', 'Staging current file', 'Installing backup'):
                self.assertIn(message, out)
        self.backup(2).write_bytes(b'older')
        self.state.update(etag='"v4"', body=b'version 4')
        out, _ = self.run_update('--enable-caching', '--keep', '1', verbose=True)
        self.assertIn('Removing expired backup', out)
        self.assertFalse(self.backup(2).exists())

    def test_verbose_explains_cache_fallbacks(self):
        self.state.update(etag='"v1"', modified=STAMP)
        self.run_update('--enable-caching')
        out, _ = self.run_update('--enable-caching', '--disable-etag', verbose=True)
        self.assertIn('Skipping ETag', out)
        self.assertIn('Using Last-Modified', out)
        self.output.write_bytes(b'local edit')
        out, _ = self.run_update('--enable-caching', verbose=True)
        self.assertIn('local file has changed', out)
        out, _ = self.run_update('--enable-caching', url=self.url + '?changed', verbose=True)
        self.assertIn('URL or request options have changed', out)
        self.meta.write_text('legacy headers\n')
        out, _ = self.run_update('--enable-caching', verbose=True)
        self.assertIn('metadata is legacy or invalid', out)
        self.output.unlink()
        out, _ = self.run_update('--enable-caching', verbose=True)
        self.assertIn('output file is missing', out)
        self.state.pop('etag')
        self.state.pop('modified')
        self.run_update('--enable-caching', '--force')
        out, _ = self.run_update('--enable-caching', verbose=True)
        self.assertIn('no usable validator', out)

    def test_verbose_and_quiet_preserve_curl_status(self):
        out, err = self.run_update()
        self.assertEqual((out, err), ('', ''))
        self.stub('curl', 'echo "injected transfer error" >&2; exit 7')
        out, err = self.run_update(verbose=True, code=2)
        self.assertIn('cURL finished with exit status 7', out)
        self.assertIn('injected transfer error', err)
        out, err = self.run_update(code=2)
        self.assertEqual(out, '')
        self.assertIn('injected transfer error', err)

    def test_download_unchanged_and_rotation(self):
        self.run_update('--keep', '2')
        initial = self.output.stat()
        self.run_update('--keep', '2')
        self.assertEqual(self.output.stat().st_ino, initial.st_ino)
        self.assertEqual(self.output.stat().st_mtime_ns, initial.st_mtime_ns)
        self.assertFalse(self.backup(1).exists())
        for data in (b'second\n', b'third\n', b'fourth\n'):
            self.state['body'] = data
            self.run_update('--keep', '2')
        self.assertEqual(self.output.read_bytes(), b'fourth\n')
        self.assertEqual(self.backup(1).read_bytes(), b'third\n')
        self.assertEqual(self.backup(2).read_bytes(), b'second\n')
        self.assertFalse(self.backup(3).exists())
        self.assertFalse(list(self.root.glob('.ibfu.*')))

    def test_rotation_gaps_and_reduced_retention(self):
        self.output.write_bytes(b'current')
        self.backup(1).write_bytes(b'one')
        self.backup(3).write_bytes(b'three')
        self.backup(10).write_bytes(b'ten')
        self.run_update('--keep', '3')
        self.assertEqual(self.backup(1).read_bytes(), b'current')
        self.assertEqual(self.backup(2).read_bytes(), b'one')
        self.assertEqual(self.backup(3).read_bytes(), b'three')
        self.assertFalse(self.backup(10).exists())
        self.state['body'] = b'next'
        self.run_update('--keep', '1')
        self.assertEqual(self.backup(1).read_bytes(), b'first\n')
        self.assertFalse(self.backup(2).exists())
        self.assertFalse(self.backup(3).exists())

    def test_etag_weak_etag_and_304_without_headers(self):
        for etag in ('"v1"', 'W/"v2"'):
            with self.subTest(etag=etag):
                self.state.update(etag=etag, modified=STAMP, headers_on_304=False)
                self.run_update('--enable-caching')
                before = self.output.stat()
                self.run_update('--enable-caching')
                self.assertEqual(self.state['requests'][-1][1]['If-None-Match'], etag)
                self.assertEqual(self.output.stat().st_ino, before.st_ino)
                self.assertIn(etag, self.meta.read_text())
                self.assertIn(STAMP, self.meta.read_text())
                self.run_update('--enable-caching', '--disable-etag')
                self.assertEqual(self.state['requests'][-1][1]['If-Modified-Since'], STAMP)

    def test_last_modified_and_remote_timestamp(self):
        self.state['modified'] = STAMP
        self.run_update('--enable-caching')
        self.assertEqual(int(self.output.stat().st_mtime), 1445412480)
        self.run_update('--enable-caching')
        self.assertEqual(self.state['requests'][-1][1]['If-Modified-Since'], STAMP)
        self.state.update(body=b'changed', modified='Wed, 21 Oct 2015 08:28:00 GMT')
        self.run_update('--disable-remote-time')
        self.assertGreater(self.output.stat().st_mtime, 1600000000)

    def test_redirect_only_uses_final_headers(self):
        self.state['etag'] = 'W/"final"'
        self.run_update('--enable-caching', url=self.url.replace('/file', '/redirect'))
        self.assertIn('W/"final"', self.meta.read_text())
        self.assertNotIn('redirect-only', self.meta.read_text())
        self.state.pop('etag')
        self.run_update('--force', '--enable-caching', url=self.url.replace('/file', '/redirect'))
        self.assertEqual(self.meta.read_text().splitlines()[2], '')

    def test_changed_etag_identical_body_updates_metadata(self):
        self.state['etag'] = '"old"'
        self.run_update('--enable-caching', '--keep', '2')
        before = self.output.stat()
        self.state['etag'] = '"new"'
        self.run_update('--enable-caching', '--keep', '2')
        self.assertEqual(self.output.stat().st_ino, before.st_ino)
        self.assertFalse(self.backup(1).exists())
        self.run_update('--enable-caching')
        self.assertEqual(self.state['requests'][-1][1]['If-None-Match'], '"new"')

    def test_force_and_disabled_caching(self):
        self.state['etag'] = '"v1"'
        self.run_update('--enable-caching')
        self.run_update('--enable-caching', '--force')
        self.assertNotIn('If-None-Match', self.state['requests'][-1][1])
        self.run_update()
        self.assertFalse(self.meta.exists())

    def test_cache_bound_to_url_request_and_local_file(self):
        self.state['etag'] = '"v1"'
        self.run_update('--enable-caching')
        self.run_update('--enable-caching', url=self.url + '?different')
        self.assertNotIn('If-None-Match', self.state['requests'][-1][1])
        self.run_update('--enable-caching', '--user-agent', 'different agent')
        self.assertNotIn('If-None-Match', self.state['requests'][-1][1])
        self.output.write_bytes(b'edited')
        self.run_update('--enable-caching', '--user-agent', 'different agent')
        self.assertNotIn('If-None-Match', self.state['requests'][-1][1])
        self.assertEqual(self.output.read_bytes(), b'first\n')

    def test_legacy_metadata_and_missing_output(self):
        self.output.write_bytes(b'old')
        self.meta.write_text('HTTP/1.1 200 OK\r\nETag: "old"\r\n\r\n')
        self.run_update('--enable-caching')
        self.assertNotIn('If-None-Match', self.state['requests'][-1][1])
        self.output.unlink()
        self.run_update('--enable-caching')
        self.assertEqual(self.output.read_bytes(), b'first\n')

    def test_unsolicited_304_preserves_file(self):
        self.output.write_bytes(b'old')
        self.state['status'] = 304
        self.run_update(code=2)
        self.assertEqual(self.output.read_bytes(), b'old')

    def test_http_errors_truncation_and_partial_content(self):
        self.output.write_bytes(b'old')
        for change in ({'status': 404}, {'status': 206}, {'status': 200, 'truncate': True}):
            with self.subTest(change=change):
                self.state.update(change)
                _, err = self.run_update('--curl-options', '--retry 0', code=2)
                self.assertTrue(err)
                self.assertEqual(self.output.read_bytes(), b'old')
                self.assertFalse(list(self.root.glob('.ibfu.*')))

    def test_curl_exit_status_and_logging(self):
        self.output.write_bytes(b'old')
        log = self.root / 'syslog'
        self.env['QA_LOG'] = str(log)
        self.stub('logger', 'printf "%s\\n" "$*" >> "$QA_LOG"')
        self.stub('curl', 'echo "injected connection failure" >&2; exit 7')
        self.run_update('--logger', code=2)
        self.assertIn('injected connection failure', log.read_text())
        self.assertEqual(self.output.read_bytes(), b'old')

    def test_gzip_and_empty_download(self):
        self.state['gzip'] = True
        self.run_update()
        self.assertEqual(self.output.read_bytes(), b'first\n')
        self.state['body'] = b''
        self.run_update()
        self.assertEqual(self.output.read_bytes(), b'')

    def test_file_protocol_and_literal_url_globs(self):
        source = self.root / 'source[1].txt'
        source.write_bytes(b'local')
        self.run_update(url=source.as_uri())
        self.assertEqual(self.output.read_bytes(), b'local')

    def test_curl_options_quotes_no_expansion_and_curlrc(self):
        self.env['SHOULD_NOT_EXPAND'] = 'expanded'
        (self.root / '.curlrc').write_text('output = "wrong-file"\nurl = "file:///does-not-exist"\n')
        options = '--header "X-Test: a b * $SHOULD_NOT_EXPAND $(echo bad)" --retry=0'
        self.run_update('--curl-options', options)
        self.assertEqual(self.state['requests'][-1][1]['X-Test'], 'a b * $SHOULD_NOT_EXPAND $(echo bad)')
        self.assertEqual(len(self.state['requests']), 1)

    def test_curl_options_reject_transfer_overrides(self):
        for options in ('--retry 0 --output x', '--next', '-vofile', '--out=x',
                        '--config x', '--url https://example.invalid/', '--head',
                        '--header "Range: bytes=1-2"', '--header @headers',
                        '--max-time', '--header "unclosed'):
            with self.subTest(options=options):
                self.run_update('--curl-options', options, code=64)
        self.assertFalse(self.state['requests'])

    def test_invalid_cli_arguments(self):
        for extra in (('',), ('--keep', '0'), ('--keep', '-1'), ('--keep', '08'),
                      ('--keep', '999999999999999999999'), ('--unknown',)):
            with self.subTest(extra=extra):
                self.run_update(*extra, code=64)

    def test_nonregular_output_and_metadata(self):
        self.output.mkdir()
        self.run_update(code=64)
        self.output.rmdir()
        self.output.symlink_to(self.root / 'missing')
        self.run_update(code=64)
        self.output.unlink()
        self.meta.mkdir()
        self.run_update(code=74)

    def test_nonregular_backup_preserves_active_file(self):
        self.output.write_bytes(b'old')
        self.backup(1).mkdir()
        self.run_update('--keep', '2', code=74)
        self.assertEqual(self.output.read_bytes(), b'old')

    def test_paths_permissions_and_keep_workdir(self):
        self.output = self.root / r'space and\n[1].dat'
        self.output.write_bytes(b'old')
        self.output.chmod(0o640)
        before = self.output.stat()
        self.run_update('--keep-working-dir')
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.output.stat().st_uid, before.st_uid)
        work, = self.root.glob('.ibfu.*')
        self.assertEqual(work.stat().st_mode & 0o777, 0o700)

    def test_setup_comparison_and_staging_failures(self):
        self.output.write_bytes(b'old')
        for command, extra in (('mktemp', ()), ('cmp', ()), ('cp', ('--keep', '1'))):
            with self.subTest(command=command):
                self.stub(command, 'echo "injected failure" >&2; exit 2')
                self.run_update(*extra, code=74)
                self.assertEqual(self.output.read_bytes(), b'old')
                (self.bin / command).unlink()

    def test_failed_publication_preserves_active_file_and_backups(self):
        self.output.write_bytes(b'old')
        self.backup(1).write_bytes(b'older')
        self.stub('mv', 'exit 1')
        self.run_update('--keep', '2', code=74)
        self.assertEqual(self.output.read_bytes(), b'old')
        self.assertEqual(self.backup(1).read_bytes(), b'older')

    def test_post_publication_failure_retains_recovery_files(self):
        self.output.write_bytes(b'old')
        self.stub('mv', 'if [[ $* == *backup.1* ]]; then exit 1; fi\nexec '
                  + shlex.quote(shutil.which('mv')) + ' "$@"')
        _, err = self.run_update('--keep', '1', code=74)
        self.assertIn('was updated', err)
        self.assertIn('recovery files remain', err)
        self.assertEqual(self.output.read_bytes(), b'first\n')
        work, = self.root.glob('.ibfu.*')
        self.assertEqual((work / 'backup.1').read_bytes(), b'old')

    def test_metadata_publication_failure_invalidates_old_cache(self):
        self.state['etag'] = '"v1"'
        self.run_update('--enable-caching')
        self.state.update(etag='"v2"', body=b'new')
        self.stub('mv', 'if [[ $* == *meta.new* ]]; then exit 1; fi\nexec '
                  + shlex.quote(shutil.which('mv')) + ' "$@"')
        _, err = self.run_update('--enable-caching', code=74)
        self.assertIn('was updated', err)
        self.assertEqual(self.output.read_bytes(), b'new')
        self.assertFalse(self.meta.exists())

    def test_concurrent_updates_serialize_through_parent_aliases(self):
        self.output.write_bytes(b'old')
        self.state['bodies'] = {'/file': b'second'}
        first = self.launch('--keep', '2', url=self.url.replace('/file', '/slow'))
        self.assertTrue(self.state['ready'].wait(3))
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        second = self.launch('--keep', '2', output=alias / self.output.name)
        time.sleep(.2)
        self.assertIsNone(second.poll())
        self.assertEqual(len(self.state['requests']), 1)
        self.assertEqual(self.output.read_bytes(), b'old')
        self.state['release'].set()
        self.finish(first)
        self.finish(second)
        self.assertEqual(self.output.read_bytes(), b'second')
        self.assertEqual(self.backup(1).read_bytes(), b'first\n')
        self.assertEqual(self.backup(2).read_bytes(), b'old')
        self.assertEqual(len(self.state['requests']), 2)

    def test_active_name_exists_until_publication(self):
        self.output.write_bytes(b'old')
        self.env['QA_OUTPUT'] = str(self.output)
        self.stub('mv', 'if [[ $* == *file.temp* ]]; then\n'
                  '  [[ $(cat "$QA_OUTPUT") == old ]] || exit 88\n'
                  'fi\nexec ' + shlex.quote(shutil.which('mv')) + ' "$@"')
        self.run_update('--keep', '2')
        self.assertEqual(self.output.read_bytes(), b'first\n')
        self.assertEqual(self.backup(1).read_bytes(), b'old')

    def test_signal_during_publication_reports_active_data(self):
        self.output.write_bytes(b'old')
        self.stub('mv', shlex.quote(shutil.which('mv')) + ' "$@" || exit $?\n'
                  'if [[ $* == *file.temp* ]]; then kill -TERM "$PPID"; fi')
        _, err = self.run_update('--keep', '1', code=143)
        self.assertIn('was updated', err)
        self.assertEqual(self.output.read_bytes(), b'first\n')
        work, = self.root.glob('.ibfu.*')
        self.assertEqual((work / 'backup.1').read_bytes(), b'old')

    def test_cleanup_failure_is_not_logged_as_success(self):
        log = self.root / 'syslog'
        self.env['QA_LOG'] = str(log)
        self.stub('logger', 'printf "%s\\n" "$*" >> "$QA_LOG"')
        self.stub('rm', 'if [[ $1 == -rf ]]; then exit 1; fi\nexec '
                  + shlex.quote(shutil.which('rm')) + ' "$@"')
        self.run_update('--logger', code=74)
        self.assertIn('was updated', log.read_text())
        self.assertNotIn('successfully updated', log.read_text())
        self.assertEqual(self.output.read_bytes(), b'first\n')

    def test_metadata_write_failure_keeps_downloaded_file(self):
        self.stub('mktemp', 'dir=$(' + shlex.quote(shutil.which('mktemp')) + ' "$@") || exit $?\n'
                  'ln -s /dev/full "$dir/meta.new"\nprintf "%s\\n" "$dir"')
        _, err = self.run_update('--enable-caching', code=74)
        self.assertIn('Unable to write metadata', err)
        self.assertEqual(self.output.read_bytes(), b'first\n')
        self.assertFalse(self.meta.exists())

    def test_header_parser_failure_keeps_old_file(self):
        self.output.write_bytes(b'old')
        self.stub('awk', 'exit 2')
        self.run_update('--enable-caching', code=74)
        self.assertEqual(self.output.read_bytes(), b'old')

    def test_signal_cleans_up_and_preserves_active_file(self):
        self.output.write_bytes(b'old')
        marker = self.root / 'ready'
        self.env['QA_READY'] = str(marker)
        self.stub('curl', 'touch "$QA_READY"; sleep 30')
        child = self.launch()
        deadline = time.monotonic() + 3
        while not marker.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        os.killpg(child.pid, signal.SIGTERM)
        self.finish(child, 143)
        self.assertEqual(self.output.read_bytes(), b'old')
        self.assertFalse(list(self.root.glob('.ibfu.*')))

    def test_timeout_cleans_up_descendants_even_after_parent_exit(self):
        for exits in (False, True):
            with self.subTest(parent_exits=exits):
                pidfile = self.root / ('child-' + str(exits))
                command = 'sleep 30 & printf "%s\\n" "$!" > "$1"; '
                command += 'exit 0' if exits else 'wait'
                child = subprocess.Popen([BASH, '-c', command, 'timeout-test', str(pidfile)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, start_new_session=True)
                self.addCleanup(self.stop, child)
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.finish(child, timeout=.2)
                pid = int(pidfile.read_text())
                proc = Path('/proc') / str(pid) / 'stat'
                deadline = time.monotonic() + 2
                while proc.exists():
                    try:
                        if proc.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                            break
                    except (FileNotFoundError, ProcessLookupError):
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)


if __name__ == '__main__':
    unittest.main()
