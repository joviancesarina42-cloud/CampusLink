import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campuslink.config import discover


class ImportDiscoveryTests(unittest.TestCase):
    def test_updated_runtime_precedes_the_previous_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            verge, data = root / 'verge', root / 'data'
            verge.mkdir()
            data.mkdir()
            runtime = verge / 'clash-verge.yaml'
            runtime.write_text('proxies: []')
            (data / 'source.yaml').write_text('old snapshot')
            with patch.dict(os.environ, {}, clear=True), \
                    patch('campuslink.config.DATA', data), \
                    patch('campuslink.config.config_dirs', return_value=[verge]):
                self.assertEqual(discover()['source'], str(runtime))
                explicit = root / 'chosen.yaml'
                explicit.write_text('chosen profile')
                self.assertEqual(discover({'source_config': str(explicit)})['source'], str(explicit))
                runtime.unlink()
                self.assertEqual(discover()['source'], str(data / 'source.yaml'))


if __name__ == '__main__':
    unittest.main()
