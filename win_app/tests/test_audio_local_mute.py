"""Muting the PC's own output while its sound plays elsewhere, and always putting it back."""

import tempfile
import unittest
from pathlib import Path

import audio_local_mute
from audio_local_mute import LocalMute


class FakeEndpoint:
    def __init__(self, muted=False, name="Speakers (Arctis 1)"):
        self.name = name
        self._muted = muted
        self.calls = []

    def muted(self):
        return self._muted

    def set_muted(self, value):
        self.calls.append(value)
        self._muted = value


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class LocalMuteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.marker = Path(self.folder.name) / "Beamer" / "muted.txt"
        self.clock = Clock()
        self.notices = []

    def tearDown(self):
        self.folder.cleanup()

    def make(self, endpoint):
        return LocalMute(endpoint, marker=self.marker, notice=lambda event, name: self.notices.append(event),
                         clock=self.clock)

    def test_streaming_mutes_and_stopping_unmutes(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.streaming(True)
        self.assertTrue(endpoint.muted())
        self.assertTrue(self.marker.exists())
        mute.streaming(True)
        self.assertEqual(endpoint.calls, [True])
        mute.streaming(False)
        self.assertFalse(endpoint.muted())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.notices, [audio_local_mute.MUTED, audio_local_mute.UNMUTED])

    def test_restore_on_the_way_out_unmutes(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.streaming(True)
        mute.restore()
        self.assertFalse(endpoint.muted())
        mute.restore()
        self.assertEqual(endpoint.calls, [True, False])

    def test_an_output_the_user_had_muted_stays_muted(self):
        endpoint = FakeEndpoint(muted=True)
        mute = self.make(endpoint)
        mute.streaming(True)
        mute.streaming(False)
        mute.restore()
        self.assertTrue(endpoint.muted())
        self.assertEqual(endpoint.calls, [])
        self.assertFalse(self.marker.exists())

    def test_unmuted_by_hand_while_streaming_is_not_muted_again(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.streaming(True)
        endpoint.set_muted(False)
        for _ in range(5):
            mute.streaming(True)
        mute.streaming(False)
        self.assertEqual(endpoint.calls, [True, False])
        self.assertEqual(len(self.notices), 1)

    def test_a_stream_that_comes_back_mutes_again(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.streaming(True)
        mute.streaming(False)
        mute.streaming(True)
        self.assertTrue(endpoint.muted())

    def test_a_mute_left_by_an_earlier_run_is_undone(self):
        endpoint = FakeEndpoint(muted=True)
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text(endpoint.name, encoding="utf-8")
        self.make(endpoint)
        self.assertFalse(endpoint.muted())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.notices, [audio_local_mute.RECOVERED])

    def test_a_marker_for_another_device_does_not_unmute_this_one(self):
        endpoint = FakeEndpoint(muted=True)
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("Speakers (Realtek)", encoding="utf-8")
        self.make(endpoint)
        self.assertTrue(endpoint.muted())
        self.assertFalse(self.marker.exists())

    def test_a_mute_that_silences_the_stream_is_undone_for_good(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.heard(9000)
        self.clock.now += 0.2
        mute.streaming(True)
        for step in range(25):
            self.clock.now += 0.1
            mute.heard(0)
        self.assertFalse(endpoint.muted())
        self.assertEqual(self.notices[-1], audio_local_mute.GAVE_UP)
        mute.streaming(False)
        mute.streaming(True)
        self.assertFalse(endpoint.muted())

    def test_sound_still_captured_after_muting_keeps_the_mute(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.heard(9000)
        mute.streaming(True)
        for step in range(40):
            self.clock.now += 0.1
            mute.heard(9000 if step % 10 else 0)
        self.assertTrue(endpoint.muted())

    def test_a_pause_long_after_muting_keeps_the_mute(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.heard(9000)
        mute.streaming(True)
        for _ in range(10):
            self.clock.now += 0.1
            mute.heard(9000)
        for _ in range(50):
            self.clock.now += 0.1
            mute.heard(0)
        self.assertTrue(endpoint.muted())

    def test_silence_when_muting_after_silence_keeps_the_mute(self):
        endpoint = FakeEndpoint()
        mute = self.make(endpoint)
        mute.heard(9000)
        self.clock.now += 5.0
        mute.streaming(True)
        for _ in range(50):
            self.clock.now += 0.1
            mute.heard(0)
        self.assertTrue(endpoint.muted())

    def test_the_marker_sits_in_beamers_folder(self):
        self.assertEqual(audio_local_mute.marker_path().parent.name, "Beamer")


if __name__ == "__main__":
    unittest.main()
