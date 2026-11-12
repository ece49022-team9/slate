import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from slate import cloud


class CloudStateTests(unittest.TestCase):
    def test_restore_keeps_skill_text_but_excludes_png_and_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "saved"
            profile = Path(directory) / "profile"
            profile.mkdir()
            (saved / "skills/example/assets").mkdir(parents=True)
            (saved / "skills/example/SKILL.md").write_text("Use this skill")
            image = saved / "skills/example/assets/image.PNG"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            (saved / "modal_snapshots.json").write_text('{"old": "im-old"}')
            (profile / "skills").mkdir()
            (profile / "skills/bundled.png").write_bytes(b"\x89PNG")
            with (
                patch.object(cloud, "SAVED", saved),
                patch.object(cloud.runtime, "PROFILE", profile),
            ):
                cloud.restore()
            self.assertEqual(
                (profile / "skills/example/SKILL.md").read_text(), "Use this skill"
            )
            self.assertFalse((profile / "skills/example/assets/image.PNG").exists())
            self.assertFalse((profile / "skills/bundled.png").exists())
            self.assertFalse((profile / "modal_snapshots.json").exists())
            self.assertTrue(image.exists())

    def test_save_does_not_add_png_to_the_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "saved"
            profile = Path(directory) / "profile"
            (profile / "skills/example").mkdir(parents=True)
            (profile / "skills/example/SKILL.md").write_text("Use this skill")
            (profile / "skills/example/image.png").write_bytes(b"\x89PNG")
            with (
                patch.object(cloud, "SAVED", saved),
                patch.object(cloud.runtime, "PROFILE", profile),
                patch.object(cloud, "state") as state,
            ):
                cloud.save()
            state.commit.assert_called_once()
            self.assertTrue((saved / "skills/example/SKILL.md").exists())
            self.assertFalse((saved / "skills/example/image.png").exists())


class CloudWarmUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_warm_up_failure_does_not_fail_startup(self):
        agent = Mock(
            run=AsyncMock(side_effect=RuntimeError("gateway failed")), close=AsyncMock()
        )
        with (
            patch.object(cloud, "Agent", return_value=agent),
            self.assertLogs("slate.cloud", level="ERROR") as logs,
        ):
            await cloud.warm_up()
        agent.close.assert_awaited_once()
        self.assertIn("Hermes warm-up failed; continuing startup", logs.output[0])

    async def test_warm_up_uses_a_fresh_session_and_reports_duration(self):
        agent = Mock(run=AsyncMock(return_value="OK"), close=AsyncMock())
        with (
            patch.object(cloud, "Agent", return_value=agent) as factory,
            self.assertLogs("slate.cloud", level="INFO") as logs,
        ):
            await cloud.warm_up()
        self.assertNotIn("session_id", factory.call_args.kwargs)
        self.assertIn("Do not use tools", factory.call_args.kwargs["instructions"])
        agent.run.assert_awaited_once_with("Reply with OK.")
        agent.close.assert_awaited_once()
        self.assertIn("Hermes warm-up took", logs.output[0])
