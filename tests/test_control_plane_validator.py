import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_control_plane import ROOT, _managed_files, _manifest_errors


class PackageManifestValidationTests(unittest.TestCase):
    def test_normal_git_checkout_excludes_git_and_runtime_files(self):
        manifest=json.loads((ROOT/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
        inventory=_managed_files(ROOT)
        self.assertFalse(any(path.startswith('.git/') for path in inventory))
        self.assertEqual(inventory,set(manifest['files']))
        self.assertEqual(_manifest_errors(ROOT,manifest),[])

    def test_missing_manifest_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'src').mkdir()
            manifest={'files':{'src/module.py':'0'*64}}
            errors=_manifest_errors(root,manifest)
            self.assertIn('manifest file missing src/module.py',errors)

    def test_modified_manifest_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'src').mkdir()
            source=root/'src'/'module.py'
            source.write_text('original\n',encoding='utf-8')
            manifest={'files':{'src/module.py':hashlib.sha256(b'expected\n').hexdigest()}}
            self.assertIn('manifest hash mismatch src/module.py',_manifest_errors(root,manifest))

    def test_unexpected_file_in_managed_scope_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'control-plane').mkdir()
            (root/'control-plane'/'identity.json').write_text('{}',encoding='utf-8')
            manifest={'files':{'control-plane/identity.json':hashlib.sha256(b'{}').hexdigest()}}
            (root/'control-plane'/'unexpected.md').write_text('extra',encoding='utf-8')
            self.assertTrue(any('manifest file set mismatch' in error and 'control-plane/unexpected.md' in error
                                for error in _manifest_errors(root,manifest)))

    def test_runtime_artifact_directories_are_narrowly_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.git').mkdir()
            (root/'.git'/'HEAD').write_text('ref: refs/heads/main',encoding='utf-8')
            (root/'.pytest_cache').mkdir()
            (root/'.pytest_cache'/'nodeids').write_text('[]',encoding='utf-8')
            (root/'reports'/'run-1').mkdir(parents=True)
            (root/'reports'/'run-1'/'report.md').write_text('generated',encoding='utf-8')
            (root/'reports'/'.gitkeep').write_text('',encoding='utf-8')
            (root/'research.sqlite').write_bytes(b'local database')
            self.assertEqual(_managed_files(root),{'reports/.gitkeep'})


if __name__=='__main__':
    unittest.main()
