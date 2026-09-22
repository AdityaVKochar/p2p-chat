from pathlib import Path
import tempfile
import unittest

from cnp2p.identity import load_or_create_identity


class IdentityTests(unittest.TestCase):
    def test_identity_is_persistent_and_well_formed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            first = load_or_create_identity(data_dir)
            second = load_or_create_identity(data_dir)

        self.assertEqual(first.node_id, second.node_id)
        self.assertEqual(first.signing_public_key, second.signing_public_key)
        self.assertEqual(first.encryption_public_key, second.encryption_public_key)
        self.assertEqual(len(first.node_id), 40)
        int(first.node_id, 16)


if __name__ == "__main__":
    unittest.main()
