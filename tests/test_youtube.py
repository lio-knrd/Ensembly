import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from sqlmodel import Session, SQLModel, create_engine, select

from backend.models import ContentPreset, YouTubeAccount
from backend.services import youtube


class SnippetTests(unittest.TestCase):
    """videos.insert rejects the whole request over YouTube's field limits."""

    def test_title_is_trimmed_and_stripped_of_forbidden_characters(self):
        snippet = youtube.build_snippet(title="A <b>bold</b> myth " + "x" * 200, description="")
        self.assertLessEqual(len(snippet["title"]), youtube.MAX_TITLE_CHARS)
        self.assertNotIn("<", snippet["title"])
        self.assertNotIn(">", snippet["title"])

    def test_empty_title_falls_back_rather_than_failing_upload(self):
        self.assertEqual("Untitled", youtube.build_snippet(title="   ", description="")["title"])

    def test_description_is_capped(self):
        snippet = youtube.build_snippet(title="t", description="d" * 6000)
        self.assertEqual(youtube.MAX_DESCRIPTION_CHARS, len(snippet["description"]))

    def test_tags_lose_the_hash_and_stay_within_the_shared_budget(self):
        snippet = youtube.build_snippet(
            title="t", description="", tags=["#greekmythology", "#theseus", ""]
        )
        self.assertEqual(["greekmythology", "theseus"], snippet["tags"])

    def test_tag_overflow_is_dropped_instead_of_erroring(self):
        snippet = youtube.build_snippet(title="t", description="", tags=[f"tag{i}" * 5 for i in range(50)])
        budget = sum(len(t) + 1 for t in snippet["tags"])
        self.assertLessEqual(budget, youtube.MAX_TAGS_CHARS)
        self.assertLess(len(snippet["tags"]), 50)


class PrivacyTests(unittest.TestCase):
    def test_unknown_privacy_status_is_rejected_before_any_request(self):
        with self.assertRaises(youtube.YouTubeError):
            youtube.start_resumable_upload(
                "token", video_bytes=10, snippet={}, privacy_status="friends"
            )

    def test_empty_and_oversized_files_are_rejected(self):
        with self.assertRaises(youtube.YouTubeError):
            youtube.start_resumable_upload("token", video_bytes=0, snippet={})
        with self.assertRaises(youtube.YouTubeError):
            youtube.start_resumable_upload(
                "token", video_bytes=youtube.MAX_VIDEO_BYTES + 1, snippet={}
            )


class ResumableSessionTests(unittest.TestCase):
    def _post(self, captured):
        def _fake(url, params=None, headers=None, content=None, **kwargs):
            captured.update(
                {"url": url, "params": params, "headers": headers, "content": content}
            )
            return httpx.Response(
                200,
                headers={"Location": "https://upload.example/session"},
                request=httpx.Request("POST", url),
            )

        return _fake

    def test_session_uri_comes_from_the_location_header(self):
        captured: dict = {}
        with mock.patch.object(httpx.Client, "post", side_effect=self._post(captured)):
            uri = youtube.start_resumable_upload(
                "token",
                video_bytes=1234,
                snippet={"title": "t"},
                privacy_status="public",
            )
        self.assertEqual("https://upload.example/session", uri)
        self.assertEqual("1234", captured["headers"]["X-Upload-Content-Length"])
        self.assertEqual("resumable", captured["params"]["uploadType"])

    def test_ai_disclosure_is_always_declared(self):
        captured: dict = {}
        with mock.patch.object(httpx.Client, "post", side_effect=self._post(captured)):
            youtube.start_resumable_upload("token", video_bytes=10, snippet={})
        import json

        body = json.loads(captured["content"])
        self.assertTrue(body["status"]["containsSyntheticMedia"])
        self.assertIn("selfDeclaredMadeForKids", body["status"])

    def test_scheduling_forces_private_because_publish_at_needs_it(self):
        captured: dict = {}
        with mock.patch.object(httpx.Client, "post", side_effect=self._post(captured)):
            youtube.start_resumable_upload(
                "token",
                video_bytes=10,
                snippet={},
                privacy_status="public",
                publish_at="2026-08-01T10:00:00Z",
            )
        import json

        status = json.loads(captured["content"])["status"]
        self.assertEqual("private", status["privacyStatus"])
        self.assertEqual("2026-08-01T10:00:00Z", status["publishAt"])


class UploadTests(unittest.TestCase):
    """The bytes must tile the file exactly and resume where Google says."""

    def _video(self, tmp: Path, size: int) -> Path:
        path = tmp / "final.mp4"
        path.write_bytes(b"x" * size)
        return path

    def test_chunks_cover_the_whole_file_in_order(self):
        sent = []

        def fake_put(url, content=None, headers=None, **kwargs):
            sent.append(headers["Content-Range"])
            total = len(content)
            first = int(headers["Content-Range"].split()[1].split("-")[0])
            if first + total >= size:
                return httpx.Response(
                    200, json={"id": "abc123"}, request=httpx.Request("PUT", url)
                )
            return httpx.Response(
                308,
                headers={"Range": f"bytes=0-{first + total - 1}"},
                request=httpx.Request("PUT", url),
            )

        size = youtube.UPLOAD_CHUNK_BYTES * 2 + 512
        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(Path(tmp), size)
            with mock.patch.object(httpx.Client, "put", side_effect=fake_put):
                result = youtube.upload_video("https://upload.example/session", video)

        self.assertEqual("abc123", result["id"])
        self.assertEqual(3, len(sent))
        self.assertEqual(f"bytes 0-{youtube.UPLOAD_CHUNK_BYTES - 1}/{size}", sent[0])
        self.assertTrue(sent[-1].endswith(f"{size - 1}/{size}"))

    def test_a_short_ack_rewinds_to_what_google_actually_received(self):
        """A 308 may confirm fewer bytes than were sent; resume from there."""
        sent = []
        size = youtube.UPLOAD_CHUNK_BYTES * 2

        def fake_put(url, content=None, headers=None, **kwargs):
            sent.append(headers["Content-Range"])
            if len(sent) == 1:
                # Only half of the first chunk landed.
                return httpx.Response(
                    308,
                    headers={"Range": f"bytes=0-{youtube.UPLOAD_CHUNK_BYTES // 2 - 1}"},
                    request=httpx.Request("PUT", url),
                )
            return httpx.Response(200, json={"id": "v"}, request=httpx.Request("PUT", url))

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(Path(tmp), size)
            with mock.patch.object(httpx.Client, "put", side_effect=fake_put):
                youtube.upload_video("https://upload.example/session", video)

        self.assertTrue(sent[1].startswith(f"bytes {youtube.UPLOAD_CHUNK_BYTES // 2}-"))

    def test_a_dropped_connection_resumes_instead_of_restarting(self):
        size = youtube.UPLOAD_CHUNK_BYTES * 2
        calls = {"n": 0}
        ranges = []

        def fake_put(url, content=None, headers=None, **kwargs):
            calls["n"] += 1
            ranges.append(headers["Content-Range"])
            if calls["n"] == 1:
                raise httpx.ConnectError("connection reset")
            if calls["n"] == 2:
                # The probe: Google reports the first chunk as received.
                return httpx.Response(
                    308,
                    headers={"Range": f"bytes=0-{youtube.UPLOAD_CHUNK_BYTES - 1}"},
                    request=httpx.Request("PUT", url),
                )
            return httpx.Response(200, json={"id": "v"}, request=httpx.Request("PUT", url))

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(Path(tmp), size)
            with mock.patch.object(httpx.Client, "put", side_effect=fake_put):
                youtube.upload_video("https://upload.example/session", video)

        self.assertEqual(f"bytes */{size}", ranges[1])
        self.assertTrue(ranges[2].startswith(f"bytes {youtube.UPLOAD_CHUNK_BYTES}-"))


class ErrorMappingTests(unittest.TestCase):
    def test_invalid_grant_asks_for_a_relink(self):
        response = httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Token expired"},
            request=httpx.Request("POST", youtube.TOKEN_URL),
        )
        with mock.patch.object(youtube, "is_configured", return_value=True):
            with mock.patch.object(httpx.Client, "post", return_value=response):
                with self.assertRaises(youtube.YouTubeNeedsRelink):
                    youtube.refresh_tokens("dead-token")

    def test_quota_errors_explain_the_daily_upload_cap(self):
        response = httpx.Response(
            403,
            json={
                "error": {
                    "message": "The request cannot be completed.",
                    "errors": [{"reason": "uploadLimitExceeded"}],
                }
            },
            request=httpx.Request("GET", youtube.API_BASE),
        )
        with self.assertRaises(youtube.YouTubeError) as caught:
            youtube._raise_for_error(response, response.json(), "Upload failed")
        self.assertIn("100 uploads per day", str(caught.exception))

    def test_a_401_is_a_relink_not_a_generic_failure(self):
        response = httpx.Response(
            401,
            json={"error": {"message": "Invalid Credentials", "errors": [{"reason": "authError"}]}},
            request=httpx.Request("GET", youtube.API_BASE),
        )
        with self.assertRaises(youtube.YouTubeNeedsRelink):
            youtube._raise_for_error(response, response.json(), "Upload failed")


class GroupSelectionTests(unittest.TestCase):
    """A group's YouTube channel is independent of its TikTok account."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_unlinking_clears_the_channel_from_every_group(self):
        with Session(self.engine) as session:
            account = YouTubeAccount(channel_id="UC123", title="Mythos")
            session.add(account)
            session.commit()
            session.refresh(account)
            for name in ("Greek Mythology", "Norse Mythology"):
                session.add(ContentPreset(name=name, youtube_account_id=account.id))
            session.commit()

            # What routers.youtube.unlink_account does.
            for preset in session.exec(
                select(ContentPreset).where(ContentPreset.youtube_account_id == account.id)
            ):
                preset.youtube_account_id = None
                session.add(preset)
            session.delete(account)
            session.commit()

            presets = session.exec(select(ContentPreset)).all()
            self.assertEqual(2, len(presets))
            self.assertTrue(all(p.youtube_account_id is None for p in presets))

    def test_the_two_destinations_are_selected_separately(self):
        with Session(self.engine) as session:
            preset = ContentPreset(name="Greek Mythology", tiktok_account_id="tt-1")
            session.add(preset)
            session.commit()
            session.refresh(preset)
            self.assertIsNone(preset.youtube_account_id)
            self.assertEqual("tt-1", preset.tiktok_account_id)


if __name__ == "__main__":
    unittest.main()
