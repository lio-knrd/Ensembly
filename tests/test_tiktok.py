import hashlib
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine, select

from backend.models import ContentPreset, TikTokAccount
from backend.services import tiktok


class ChunkPlanTests(unittest.TestCase):
    """TikTok's upload rules: 5-64 MB chunks, whole-file under 5 MB."""

    def test_small_file_uploads_whole(self):
        size = 3 * tiktok.MB
        self.assertEqual((size, 1), tiktok.chunk_plan(size))

    def test_chunk_count_floors_so_the_last_chunk_takes_the_remainder(self):
        size = 25 * tiktok.MB  # 10 MB chunks -> 2 chunks, last one 15 MB
        chunk_size, total = tiktok.chunk_plan(size)
        self.assertEqual(10 * tiktok.MB, chunk_size)
        self.assertEqual(2, total)
        self.assertLessEqual(chunk_size, tiktok.MAX_CHUNK)
        self.assertGreaterEqual(chunk_size, tiktok.MIN_CHUNK)

    def test_huge_file_stays_within_the_chunk_ceiling(self):
        chunk_size, total = tiktok.chunk_plan(3 * 1024 * tiktok.MB)
        self.assertLessEqual(total, tiktok.MAX_CHUNKS)
        self.assertLessEqual(chunk_size, tiktok.MAX_CHUNK)

    def test_oversized_and_empty_files_are_rejected(self):
        with self.assertRaises(tiktok.TikTokError):
            tiktok.chunk_plan(0)
        with self.assertRaises(tiktok.TikTokError):
            tiktok.chunk_plan(tiktok.MAX_VIDEO_BYTES + 1)


class UploadRangeTests(unittest.TestCase):
    """Chunks must tile the file exactly, with the last one taking the rest."""

    def _capture_upload(self, total_bytes: int, chunk_size: int, total_chunks: int):
        import tempfile
        from pathlib import Path

        sent = []

        class FakeResponse:
            status_code = 200
            text = ""

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def put(self, url, content, headers):
                sent.append((headers["Content-Range"], len(content), content))
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            payload = bytes(range(256)) * (total_bytes // 256 + 1)
            path.write_bytes(payload[:total_bytes])
            with mock.patch.object(tiktok.httpx, "Client", lambda *a, **k: FakeClient()):
                tiktok.upload_video("https://upload", path, chunk_size, total_chunks)
            return sent, path.read_bytes()

    def test_ranges_cover_every_byte_once(self):
        total = 25 * tiktok.MB
        chunk_size, total_chunks = tiktok.chunk_plan(total)
        sent, original = self._capture_upload(total, chunk_size, total_chunks)

        self.assertEqual(total_chunks, len(sent))
        self.assertEqual(f"bytes 0-{10 * tiktok.MB - 1}/{total}", sent[0][0])
        # The final chunk carries the 15 MB remainder, not just chunk_size.
        self.assertEqual(f"bytes {10 * tiktok.MB}-{total - 1}/{total}", sent[-1][0])
        self.assertEqual(original, b"".join(chunk for _, _, chunk in sent))

    def test_small_file_goes_up_in_one_range(self):
        total = 1024
        sent, original = self._capture_upload(total, total, 1)

        self.assertEqual(1, len(sent))
        self.assertEqual(f"bytes 0-{total - 1}/{total}", sent[0][0])
        self.assertEqual(original, sent[0][2])


class PkceTests(unittest.TestCase):
    def test_challenge_is_the_hex_sha256_tiktok_expects(self):
        verifier = "a" * 64
        expected = hashlib.sha256(verifier.encode("ascii")).hexdigest()
        self.assertEqual(expected, tiktok.pkce_challenge(verifier))


class AuthorizeUrlTests(unittest.TestCase):
    def test_authorize_url_carries_the_app_credentials_and_state(self):
        with mock.patch.object(tiktok.settings, "tiktok_client_key", "key123"), \
            mock.patch.object(tiktok.settings, "tiktok_client_secret", "secret"), \
            mock.patch.object(tiktok.settings, "tiktok_redirect_uri", "https://example.com/cb"), \
            mock.patch.object(tiktok.settings, "tiktok_use_pkce", False):
            start = tiktok.start_authorization()

        self.assertIn("client_key=key123", start.url)
        self.assertIn("response_type=code", start.url)
        self.assertIn("video.publish", start.url)
        self.assertIn(f"state={start.state}", start.url)
        self.assertNotIn("code_challenge", start.url)

    def test_pkce_challenge_is_added_for_desktop_clients(self):
        with mock.patch.object(tiktok.settings, "tiktok_client_key", "key123"), \
            mock.patch.object(tiktok.settings, "tiktok_client_secret", "secret"), \
            mock.patch.object(tiktok.settings, "tiktok_redirect_uri", "https://example.com/cb"), \
            mock.patch.object(tiktok.settings, "tiktok_use_pkce", True):
            start = tiktok.start_authorization()

        self.assertIn(f"code_challenge={tiktok.pkce_challenge(start.code_verifier)}", start.url)
        self.assertIn("code_challenge_method=S256", start.url)

    def test_missing_credentials_are_reported_not_guessed(self):
        with mock.patch.object(tiktok.settings, "tiktok_client_key", ""), \
            mock.patch.object(tiktok.settings, "tiktok_client_secret", ""):
            with self.assertRaises(tiktok.TikTokNotConfigured):
                tiktok.start_authorization()


class AccountSharingTests(unittest.TestCase):
    """One linked account can back several groups; unlinking clears them all."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_two_groups_can_select_the_same_account(self):
        from backend.routers.tiktok import _groups_using, unlink_account

        with Session(self.engine) as session:
            account = TikTokAccount(open_id="open-1", display_name="Mythforge")
            session.add(account)
            session.commit()
            session.refresh(account)
            session.add(ContentPreset(name="Greek Mythology", tiktok_account_id=account.id))
            session.add(ContentPreset(name="Cat Cartoon", tiktok_account_id=account.id))
            session.add(ContentPreset(name="Math"))
            session.commit()

            self.assertEqual(
                ["Cat Cartoon", "Greek Mythology"],
                sorted(g["name"] for g in _groups_using(session, account.id)),
            )

            unlink_account(account.id, session)

            remaining = session.exec(select(ContentPreset)).all()
            self.assertTrue(all(p.tiktok_account_id is None for p in remaining))


if __name__ == "__main__":
    unittest.main()
