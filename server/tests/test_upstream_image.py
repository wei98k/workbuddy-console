"""Remote upstream image updates preserve base compose and fail closed on pull."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'registry.cn-shenzhen.aliyuncs.com/xdock/workbuddy2api:latest'


class UpstreamImageTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('update_image_test', ROOT / 'deploy/update.py')
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.upstream = self.root / 'upstream'
        self.deploy = self.root / 'custom-deploy'
        self.upstream.mkdir()
        self.deploy.mkdir()
        self.base = self.upstream / 'docker-compose.yml'
        self.base.write_text('services:\n  workbuddy2api:\n    networks: [custom]\n', encoding='utf-8')
        self.overlay = self.deploy / 'upstream-image.compose.yml'
        self.overlay.write_text(f'services:\n  workbuddy2api:\n    image: {IMAGE}\n', encoding='utf-8')
        self.worker.UPSTREAM_DIR = self.upstream
        self.worker.__file__ = str(self.deploy / 'update.py')
        self.rep = mock.Mock()

    def command(self, *args, compose=None):
        return (compose or ['docker', 'compose']) + [
            '-f', str(self.base), '-f', str(self.overlay), *args]

    def test_pull_then_start_using_script_relative_override_without_build(self):
        original = self.base.read_bytes()
        (self.upstream / 'Dockerfile').write_text('COPY missing-file /app/\n', encoding='utf-8')
        with mock.patch.object(self.worker, '_compose_cmd', return_value=['docker', 'compose']), \
                mock.patch.object(self.worker, 'run', return_value=(0, '')) as run, \
                mock.patch.object(self.worker, '_missing_copy_sources', side_effect=AssertionError('no local preflight')), \
                mock.patch.object(self.worker, 'wait_health', return_value=True) as health:
            self.worker.rebuild_upstream(self.rep)
        self.assertEqual(run.call_args_list, [
            mock.call(self.command('pull', 'workbuddy2api'), cwd=self.upstream, rep=self.rep, check=False),
            mock.call(self.command('up', '-d', '--no-build', 'workbuddy2api'), cwd=self.upstream, rep=self.rep, check=False),
        ])
        health.assert_called_once()
        self.assertEqual(self.base.read_bytes(), original)

    def test_pull_failure_does_not_start_or_wait(self):
        with mock.patch.object(self.worker, '_compose_cmd', return_value=['docker', 'compose']), \
                mock.patch.object(self.worker, 'run', return_value=(1, 'unauthorized: registry access denied')) as run, \
                mock.patch.object(self.worker, 'wait_health') as health:
            with self.assertRaisesRegex(RuntimeError, '镜像拉取失败.*unauthorized: registry access denied'):
                self.worker.rebuild_upstream(self.rep)
        run.assert_called_once_with(self.command('pull', 'workbuddy2api'), cwd=self.upstream, rep=self.rep, check=False)
        health.assert_not_called()

    def test_start_failure_preserves_real_error(self):
        with mock.patch.object(self.worker, '_compose_cmd', return_value=['docker-compose']), \
                mock.patch.object(self.worker, 'run', side_effect=[(0, ''), (2, 'network custom not found')]) as run, \
                mock.patch.object(self.worker, 'wait_health') as health:
            with self.assertRaisesRegex(RuntimeError, '启动失败.*exit 2.*network custom not found'):
                self.worker.rebuild_upstream(self.rep)
        self.assertEqual(run.call_args_list[-1].args[0], self.command('up', '-d', '--no-build', 'workbuddy2api', compose=['docker-compose']))
        health.assert_not_called()

    def test_missing_override_is_reported_without_docker_commands(self):
        self.worker.__file__ = str(self.root / 'missing-deploy' / 'update.py')
        with mock.patch.object(self.worker, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '未找到 compose 配置文件.*upstream-image.compose.yml'):
                self.worker.rebuild_upstream(self.rep)
        run.assert_not_called()

    def test_distributed_override_selects_requested_image(self):
        text = (ROOT / 'deploy/upstream-image.compose.yml').read_text(encoding='utf-8')
        self.assertIn(f'image: {IMAGE}', text)
        self.assertIn('workbuddy2api:', text)
        self.assertNotIn('build:', text)

    def test_deploy_sync_carries_override_beside_updater(self):
        install = self.root / 'install'
        (install / 'deploy').mkdir(parents=True)
        (install / 'deploy/update.py').write_text('# previous updater\n', encoding='utf-8')
        (self.deploy / 'update.py').write_text('# new updater\n', encoding='utf-8')
        self.worker._sync_deploy(self.deploy, install, self.root / 'backup', self.rep)
        self.assertEqual((install / 'deploy/upstream-image.compose.yml').read_bytes(), self.overlay.read_bytes())
        self.assertEqual((install / 'deploy/update.py').read_bytes(), (self.deploy / 'update.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
