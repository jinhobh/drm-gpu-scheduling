import hashlib
from pathlib import Path
import tempfile
import unittest

import share_fair


class ShareTest(unittest.TestCase):
    def fixture(self, root):
        entries = {}
        folder = root / 'traces/run'
        folder.mkdir(parents=True)
        for name in share_fair.FILES:
            data = name.encode()
            (folder / name).write_bytes(data)
            entries[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        return dict(runs=[dict(path='traces/run', files=entries)])

    def test_only_hash_matched_allowlisted_files_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.fixture(root)
            self.assertEqual(len(share_fair.validated_inputs(root, manifest)), 5)
            (root / 'traces/run/trace.dat').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'hash/size'):
                share_fair.validated_inputs(root, manifest)

    def test_extra_logs_and_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.fixture(root)
            manifest['runs'][0]['files']['clients.jsonl'] = {}
            with self.assertRaisesRegex(ValueError, 'allowlist'):
                share_fair.validated_inputs(root, manifest)
            del manifest['runs'][0]['files']['clients.jsonl']
            manifest['runs'][0]['path'] = '../outside'
            with self.assertRaisesRegex(ValueError, 'escapes'):
                share_fair.validated_inputs(root, manifest)
