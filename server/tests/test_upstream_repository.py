"""Offline regression checks for the configured workbuddy2api repository.

Run from the repository root:
    uv run --offline --no-project python -m unittest server.tests.test_upstream_repository -v

Only pure configuration AST fragments are executed; deployment scripts are never
sourced. The release check uses a temporary fake git, not GitHub or a deployment.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SSH_REPO = 'git@github.com:wei98k/workbuddy2api.git'
API_SLUG = 'wei98k/workbuddy2api'
WEB_REPO = 'https://github.com/wei98k/workbuddy2api'


def _pure_config(path: str, names: set[str], functions: set[str] | None = None) -> dict:
    """Load selected assignments/functions without importing an application."""
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'), filename=path)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in names:
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in (functions or set()):
            nodes.append(node)
    namespace = {'os': os, 're': re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)
    return namespace


class UpstreamRepository(unittest.TestCase):
    def test_shell_clone_defaults_match_origin(self) -> None:
        for path in ('deploy/install.sh', 'deploy/check-upstream.sh'):
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding='utf-8')
                match = re.search(
                    r'^UPSTREAM_REPO="\$\{UPSTREAM_REPO:-([^}]+)\}"\s*$',
                    source, re.MULTILINE,
                )
                self.assertIsNotNone(match, 'expected an environment-overridable clone URL')
                self.assertEqual(match.group(1), SSH_REPO)

    def test_worker_clone_default_and_environment_override(self) -> None:
        for value, expected in (('', SSH_REPO), ('git@example.test:owner/fork.git',
                                                'git@example.test:owner/fork.git')):
            with self.subTest(value=value), patch.dict(os.environ, {'WB_UPSTREAM_REPO': value}):
                config = _pure_config('deploy/update.py', {'UPSTREAM_REPO'})
                self.assertEqual(config['UPSTREAM_REPO'], expected)

    def test_api_default_and_ssh_normalization(self) -> None:
        cases = (
            ('', '', API_SLUG),
            (SSH_REPO, '', API_SLUG),
            ('  ' + SSH_REPO + '  ', '', API_SLUG),
            ('ssh://git@github.com/wei98k/workbuddy2api.git', '', API_SLUG),
            (WEB_REPO + '.git', '', API_SLUG),
            ('git@github.com:example/fork.git', '', 'example/fork'),
            ('', 'example/custom', 'example/custom'),
        )
        for repo, api_repo, expected in cases:
            with self.subTest(repo=repo, api_repo=api_repo), patch.dict(os.environ, {
                'WB_UPSTREAM_REPO': repo, 'WB_UPSTREAM_API_REPO': api_repo,
            }):
                config = _pure_config(
                    'server/services/updater.py',
                    {'UPSTREAM_API_REPO', '_upstream_api_repo_cache'},
                    {'_upstream_api_slug'},
                )
                if not api_repo:
                    self.assertEqual(config['UPSTREAM_API_REPO'], API_SLUG)
                self.assertEqual(config['_upstream_api_slug'](), expected)
                self.assertEqual(config['_upstream_api_slug'](), expected, 'cached slug differs')

    def test_frontend_upstream_link(self) -> None:
        source = (ROOT / 'web/components/common/layout/ManagementBar.tsx').read_text(
            encoding='utf-8',
        )
        self.assertRegex(source, r'href=[\'"]' + re.escape(WEB_REPO) + r'[\'"]')

    def test_repository_has_no_obsolete_owner_paths(self) -> None:
        # Include source, docs/archives and non-ignored new files, but not .git,
        # virtualenvs or build outputs. Do not ban the original author's name.
        result = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
            cwd=ROOT, capture_output=True, check=True,
        )
        obsolete = re.compile(r'sliverkiss' + r'[/\\:]workbuddy2api', re.IGNORECASE)
        offenders = []
        for name in sorted(set(os.fsdecode(result.stdout).split('\0')) - {''}):
            path = ROOT / name
            if not path.is_file() or path.is_symlink():
                continue
            try:
                source = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(source.splitlines(), 1):
                if obsolete.search(line):
                    offenders.append(f'{name}:{number}')
        self.assertEqual(offenders, [], 'obsolete upstream repository references remain')

    def test_original_author_copyright_is_preserved(self) -> None:
        archive_source = (ROOT / 'dev/archive_upstream_snapshot.py').read_text(
            encoding='utf-8',
        )
        self.assertIn('原项目版权归 Sliverkiss', archive_source)
        self.assertIn('MIT 许可', archive_source)

    def test_release_check_fails_closed_when_clone_fails(self) -> None:
        bash = shutil.which('bash')
        self.assertIsNotNone(bash)
        errors = (
            'fatal: unable to access repository: HTTP 404',
            'ERROR: Repository not found.',
            'fatal: Could not resolve host: github.com',
        )
        for error in errors:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                fake_git = directory / 'git'
                fake_git.write_text(
                    '#!/bin/sh\n'
                    'printf "%s\\n" "$@" > "$FAKE_GIT_ARGS"\n'
                    'printf "%s\\n" "$FAKE_GIT_ERROR" >&2\n'
                    'exit 128\n', encoding='utf-8',
                )
                fake_git.chmod(0o755)
                args_file = directory / 'git-args'
                env = dict(os.environ)
                env.pop('UPSTREAM_REPO', None)
                env.update({
                    'PATH': str(directory) + os.pathsep + env.get('PATH', os.defpath),
                    'TMPDIR': str(directory),
                    'FAKE_GIT_ARGS': str(args_file),
                    'FAKE_GIT_ERROR': error,
                })
                result = subprocess.run(
                    [bash, str(ROOT / 'deploy/check-upstream.sh'), 'deadbeef'],
                    cwd=directory, env=env, capture_output=True, text=True,
                    encoding='utf-8', errors='backslashreplace', timeout=10,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2, output)
                args = args_file.read_text(encoding='utf-8').splitlines()
                self.assertEqual(args[0], 'clone')
                self.assertIn(SSH_REPO, args)
                self.assertNotIn('直接发版即可', output)
                self.assertFalse((directory / 'up').exists())


if __name__ == '__main__':
    unittest.main()
