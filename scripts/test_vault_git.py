"""Real temporary-Git checkpoint tests and isolated private-push checks."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_git


class VaultGitTests(unittest.TestCase):
    def test_checkpoint_preserves_other_staged_work_and_uses_repo_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], text=True).strip()
            git('init', '-q'); git('config', 'user.name', 'Test Author')
            git('config', 'user.email', 'author@example.test')
            for name in ('mine.md', 'other.md'):
                (Path(tmp)/name).write_text('initial')
            git('add', 'mine.md', 'other.md'); git('commit', '-qm', 'initial')
            (Path(tmp)/'other.md').write_text('another session'); git('add', 'other.md')
            (Path(tmp)/'mine.md').write_text('owned change')
            sha = vault_git.commit_owned(tmp, ['mine.md'], 'owned checkpoint')
            self.assertTrue(sha)
            self.assertEqual(git('show', '--format=', '--name-only', 'HEAD'), 'mine.md')
            self.assertEqual(git('diff', '--cached', '--name-only'), 'other.md')
            self.assertEqual(git('show', 'HEAD:other.md'), 'initial')
            self.assertEqual(git('log', '-1', '--format=%ae'), 'author@example.test')
            self.assertIsNone(vault_git.commit_owned(tmp, ['mine.md'], 'no changes'))

    def test_absolute_and_parent_paths_are_rejected(self):
        for path in ('C:/elsewhere/note.md', '../note.md', '/note.md', ':(glob)*'):
            with self.assertRaises(vault_git.BackupError):
                vault_git.commit_owned('.', [path], 'test')

    def scenario(self, enabled='true', url='https://github.com/owner/vault.git',
                 private=True, remote_name='owner/vault', branch='master', default='master', auth_error=False):
        calls = []
        def command(vault, *args, **kwargs):
            calls.append(args)
            if args[:2] == ('git', 'config'):return enabled
            if args[:2] == ('git', 'remote'):return url
            if args[:3] == ('gh', 'repo', 'view'):
                if auth_error:raise vault_git.BackupError('Authentication failed')
                return json.dumps({'nameWithOwner':remote_name,'isPrivate':private,
                                   'defaultBranchRef':{'name':default}})
            if args[:2] == ('git', 'symbolic-ref'):return branch
            if args[:2] == ('git', 'rev-parse'):return 'a'*40
            if args[:2] == ('git', 'push'):return 'up to date'
            raise AssertionError(args)
        return calls, command

    def test_private_target_push_uses_verified_url_and_frozen_commit(self):
        calls, command = self.scenario()
        with patch.object(vault_git, 'run', side_effect=command):
            self.assertEqual(vault_git.push_private('.'), 'pushed')
        self.assertIn(('git','rev-parse','refs/heads/master'), calls)
        self.assertEqual(calls[-1], ('git','push','--porcelain','--',
                                     'https://github.com/owner/vault.git','a'*40+':refs/heads/master'))

    def test_disabled_backup_makes_no_network_calls(self):
        calls, command = self.scenario(enabled='false')
        with patch.object(vault_git, 'run', side_effect=command):
            self.assertEqual(vault_git.push_private('.'), 'disabled')
        self.assertEqual(len(calls), 1)

    def test_public_unknown_or_wrong_target_never_pushes(self):
        for config in ({'private':False}, {'url':'https://example.com/owner/vault.git'},
                       {'remote_name':'someone/else'}, {'auth_error':True},
                       {'branch':'unfinished-work'}, {'private':'true'}):
            calls, command = self.scenario(**config)
            with patch.object(vault_git, 'run', side_effect=command), self.assertRaises(vault_git.BackupError):
                vault_git.push_private('.')
            self.assertFalse(any(c[:2] == ('git','push') for c in calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
