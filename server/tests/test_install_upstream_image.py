"""Installer uses the remote upstream image without losing Compose customizations."""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class InstallUpstreamImageTest(unittest.TestCase):
    def test_installer_passes_both_compose_files_as_separate_arguments(self):
        script = (ROOT / 'deploy/install.sh').read_text(encoding='utf-8')
        helper = script.split('upstream_compose() {', 1)[1].split('\n}', 1)[0]
        with tempfile.TemporaryDirectory(prefix='upstream image ') as tmp:
            result = subprocess.run(
                ['bash', '-c', 'COMPOSE="record compose"; '
                 'record() { printf "%s\\n" "$@"; }; '
                 'UPSTREAM_DIR="$1"; SRC_DIR="$2"; '
                 'upstream_compose() {' + helper + '\n}; '
                 'upstream_compose up -d --no-build workbuddy2api',
                 'test', tmp, str(ROOT)],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(result.stdout.splitlines(), [
                'compose', '-f', str(pathlib.Path(tmp) / 'docker-compose.yml'),
                '-f', str(ROOT / 'deploy/upstream-image.compose.yml'),
                'up', '-d', '--no-build', 'workbuddy2api',
            ])

    def test_new_and_existing_installations_pull_before_starting(self):
        script = (ROOT / 'deploy/install.sh').read_text(encoding='utf-8')
        upstream = script.split('# ── 1. 安装上游', 1)[1].split('# ── 2. 安装管理端', 1)[0]
        self.assertEqual(upstream.count('upstream_compose pull workbuddy2api'), 2)
        self.assertEqual(upstream.count('upstream_compose up -d --no-build workbuddy2api'), 2)
        self.assertNotIn('up -d --build', upstream)
        for branch in upstream.split('upstream_compose pull workbuddy2api')[1:]:
            self.assertTrue(branch.lstrip().startswith('upstream_compose up -d --no-build workbuddy2api'))

    @unittest.skipUnless(shutil.which('docker'), 'Docker CLI unavailable')
    def test_compose_merge_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / 'docker-compose.yml'
            base.write_text('''services:
  workbuddy2api:
    build: .
    image: old-upstream:latest
    ports: ["127.0.0.1:7863:7863"]
    volumes: ["./auths:/app/auths"]
    environment:
      TZ: Asia/Shanghai
    networks: [custom]
networks:
  custom:
    external: true
''', encoding='utf-8')
            result = subprocess.run([
                'docker', 'compose', '-f', str(base), '-f',
                str(ROOT / 'deploy/upstream-image.compose.yml'),
                'config', '--format', 'json',
            ], capture_output=True, text=True, check=True)
            config = json.loads(result.stdout)
            service = config['services']['workbuddy2api']
            self.assertEqual(service['image'], 'registry.cn-shenzhen.aliyuncs.com/xdock/workbuddy2api:latest')
            self.assertEqual(service['ports'][0]['host_ip'], '127.0.0.1')
            self.assertEqual(service['volumes'][0]['target'], '/app/auths')
            self.assertEqual(service['environment']['TZ'], 'Asia/Shanghai')
            self.assertIn('custom', service['networks'])
            self.assertTrue(config['networks']['custom']['external'])

    def test_override_changes_only_the_upstream_image(self):
        override = (ROOT / 'deploy/upstream-image.compose.yml').read_text(encoding='utf-8')
        lines = [line for line in override.splitlines() if line.strip() and not line.startswith('#')]
        self.assertEqual(lines, [
            'services:', '  workbuddy2api:',
            '    image: registry.cn-shenzhen.aliyuncs.com/xdock/workbuddy2api:latest',
        ])


if __name__ == '__main__':
    unittest.main()
