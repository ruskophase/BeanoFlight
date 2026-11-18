"""Exercise real dashboard JavaScript in Chromium, without a server or RAW files."""
import errno
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from importlib.resources import files
from pathlib import Path


class BrowserDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        for attempt in range(10):
            try:
                return super().cleanup()
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EBUSY) or attempt == 9:
                    raise
                time.sleep(.2)


@unittest.skipUnless(shutil.which('chromium'), 'Chromium is needed for browser regression tests')
class DashboardNavigationTests(unittest.TestCase):
    def test_viewing_preferences_survive_navigation(self):
        self._exercise('dashboard_navigation.js')

    def test_chart_interpretations(self):
        fields = ['bean_id', 'sequence', 'first_frame', 'lightness', 'caml_area', 'camr_area', 'area_ratio', 'lightness_delta']
        beans = [[f'test/{i}', i, i * 10, 42, 10, 20 if i < 12 else 5, .5 if i < 12 else 2, -2 if i < 12 else 3]
                 for i in range(20)]
        samples = [[f'test/{i}', i, height, 1, focus, area]
                   for i in range(20) for height, focus, area in [(50, 2, .5), (500, .5, 2), (1030, 1.5, 1)]]
        for scenario in ('mixed', 'equal', 'missing', 'sparse'):
            with self.subTest(scenario=scenario):
                payload = {'fields': fields, 'beans': beans, 'stereo_samples': samples, 'test_case': scenario}
                if scenario == 'equal':
                    payload['beans'] = [[f'test/{i}', i, i * 10, 42, 10, 10, 1, 0] for i in range(20)]
                    payload['stereo_samples'] = [[f'test/{i}', i, 50, 1, 1, 1] for i in range(20)]
                elif scenario == 'missing':
                    payload['beans'] = [[f'test/{i}', i, i * 10, 42, None, None, None, None] for i in range(20)]
                    payload['stereo_samples'] = [[f'test/{i}', i, 50, None, None, None] for i in range(20)]
                elif scenario == 'sparse':
                    payload['beans'] = beans[:3]
                    payload['stereo_samples'] = samples[:9]
                self._exercise('dashboard_interpretations.js', payload)

    def test_standalone_camera_interpretations(self):
        for chart in ('area-scatter', 'area-ratio', 'lightness-delta'):
            with self.subTest(chart=chart):
                self._exercise('dashboard_interpretations.js', {
                    'fields': ['bean_id', 'sequence', 'caml_area', 'camr_area', 'area_ratio', 'lightness_delta'],
                    'beans': [['test/1', 1, 10, 20, 2, 3]], 'test_case': 'standalone'}, chart)

    def _exercise(self, harness_name, extra_payload=None, chart=None):
        # Keep the fixture outside /tmp, which snap Chromium can isolate.
        with BrowserDirectory(prefix='dashboard-test-', dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve()
            assets = files('beanoflight').joinpath('dashboard_assets')
            for name in ('dashboard.js', 'dashboard.css'):
                (root / name).write_bytes(assets.joinpath(name).read_bytes())
            # A valid small PNG suffices: this test exercises UI state, not imaging.
            photo = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC'
                     'AAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII=')
            fields = ['bean_id', 'sequence', 'first_frame', 'lightness', 'photo_CamL', 'photo_CamR']
            payload = {
                'schema': 'beanoflight-statistics-dashboard/v1', 'fields': fields,
                'beans': [[f'test/{i}', i, i * 10, 42, photo + f'#{i}-1-L', photo + f'#{i}-1-R']
                          for i in range(1, 4)],
                'sample_pairs': {f'test/{i}': [
                    {'CamL': photo + f'#{i}-{s}-L', 'CamR': photo + f'#{i}-{s}-R',
                     'full_CamL': photo + f'#frame-{i}-{s}-L', 'full_CamR': photo + f'#frame-{i}-{s}-R',
                     'frame_index': i * 10 + s, 'right_frame_index': i * 10 + s}
                    for s in (1, 2)] for i in range(1, 4)},
                'summary': {'source_run_id': 'navigation-test'}, 'source_fps': 60, 'first_frame': 0,
                'image_policy': {'images_available': True, 'portable_full_frames': True},
            }
            payload.update(extra_payload or {})
            (root / 'batch-data.js').write_text('window.BEANO_BATCH_DATA=' + json.dumps(payload) + ';')
            harness = Path(__file__).with_name(harness_name).read_text()
            page = assets.joinpath('chart.html' if chart else 'index.html').read_text().replace('</body>', '<script>' + harness + '</script></body>')
            (root / 'index.html').write_text(page)
            completed = subprocess.run([
                'chromium', '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                '--disable-background-networking', '--disable-component-update', '--no-first-run',
                '--no-default-browser-check', '--user-data-dir=' + str(root / 'profile'),
                '--dump-dom', '--virtual-time-budget=1000', (root / 'index.html').as_uri() + ('#' + chart if chart else ''),
            ], capture_output=True, text=True, timeout=45)
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            self.assertIn('id="navigation-test-result" data-status="passed"', completed.stdout,
                          completed.stdout[-5000:] + completed.stderr[-1000:])


if __name__ == '__main__':
    unittest.main()
