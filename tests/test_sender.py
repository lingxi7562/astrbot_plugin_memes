import tempfile
import unittest
from pathlib import Path

from backend.sender import MemeSender, SendPipelineSettings


class _Event:
    def __init__(self, *, chain=True, image=True, failures=0):
        self.sent = []
        self.failures = failures
        if chain:
            self.chain_result = lambda parts: ("chain", parts)
        if image:
            self.image_result = lambda path: ("image", path)

    async def send(self, payload):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary send failure")
        self.sent.append(payload)


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_chain_mode_uses_injected_image_factory(self):
        event = _Event(chain=True, image=False)
        sender = MemeSender(
            SendPipelineSettings(mode="chain"),
            image_factory=lambda path: ("fake-image", path),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meme.png"
            path.write_bytes(b"image")
            await sender.send(event, path)
        self.assertEqual(event.sent[0][0], "chain")
        self.assertEqual(event.sent[0][1][0], ("fake-image", str(path)))

    async def test_image_result_mode_supports_sync_result(self):
        event = _Event(chain=False, image=True)
        sender = MemeSender(SendPipelineSettings(mode="image_result"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meme.webp"
            path.write_bytes(b"image")
            await sender.send(event, path)
        self.assertEqual(event.sent, [("image", str(path))])

    async def test_auto_falls_back_to_image_result(self):
        event = _Event(chain=False, image=True)
        sender = MemeSender(SendPipelineSettings(mode="auto"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meme.gif"
            path.write_bytes(b"image")
            await sender.send(event, path)
        self.assertEqual(event.sent, [("image", str(path))])

    async def test_retry_is_bounded_and_eventually_succeeds(self):
        event = _Event(chain=False, image=True, failures=1)
        sender = MemeSender(SendPipelineSettings(mode="image_result", retry_count=1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meme.jpg"
            path.write_bytes(b"image")
            await sender.send(event, path)
        self.assertEqual(len(event.sent), 1)

    async def test_missing_path_fails_before_send(self):
        event = _Event()
        sender = MemeSender()
        with self.assertRaises(FileNotFoundError):
            await sender.send(event, "does-not-exist.png")


class SenderSettingsTests(unittest.TestCase):
    def test_safe_clamps_untrusted_values(self):
        settings = SendPipelineSettings.safe("bad", 999, -5)
        self.assertEqual(settings, SendPipelineSettings())


if __name__ == "__main__":
    unittest.main()
