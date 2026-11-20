"""The experimental audio stream: its handshake, its packets and the jitter buffer."""

import base64
import os
import struct
import threading
import time
import unittest

from core import audio_stream as audio
from core import protocol

PACKET = audio.PACKET_BYTES


def token():
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


def pcm(number):
    return bytes([number % 256]) * PACKET


class Collector:
    def __init__(self):
        self.packets = []

    def push(self, seq, data):
        self.packets.append((seq, data))

    def reset(self):
        self.packets.append("reset")


def handshake(shared_token, receiver_peers=None, now=0.0):
    sender = audio.SenderSession(shared_token)
    buffer = Collector()
    receiver = audio.ReceiverCore(receiver_peers or {shared_token: "Studio PC"}, buffer)
    reply = receiver.handle(sender.hello(), now)
    return sender, receiver, buffer, reply


class HandshakeTests(unittest.TestCase):
    def test_a_paired_sender_is_answered_and_its_audio_arrives_in_order(self):
        sender, receiver, buffer, reply = handshake(token())
        self.assertTrue(sender.accept_reply(reply))
        for number in range(1, 4):
            receiver.handle(sender.seal(pcm(number)), 0.1)
        self.assertEqual(buffer.packets, ["reset", (1, pcm(1)), (2, pcm(2)), (3, pcm(3))])
        self.assertEqual(receiver.playing, "Studio PC")

    def test_a_microphone_stream_and_a_sound_stream_never_answer_each_other(self):
        shared = token()
        for sender_salt, receiver_salt in ((audio.MIC_SALT, audio.AUDIO_SALT), (audio.AUDIO_SALT, audio.MIC_SALT)):
            sender = audio.SenderSession(shared, salt=sender_salt)
            receiver = audio.ReceiverCore({shared: "Studio PC"}, Collector(), salt=receiver_salt)
            self.assertFalse(sender.accept_reply(receiver.handle(sender.hello(), 0.0)))

    def test_a_microphone_stream_works_under_its_own_salt(self):
        shared = token()
        sender = audio.SenderSession(shared, salt=audio.MIC_SALT)
        buffer = Collector()
        receiver = audio.ReceiverCore({shared: "Studio PC"}, buffer, salt=audio.MIC_SALT)
        self.assertTrue(sender.accept_reply(receiver.handle(sender.hello(), 0.0)))
        receiver.handle(sender.seal(pcm(1)), 0.1)
        self.assertEqual(buffer.packets, ["reset", (1, pcm(1))])
        self.assertNotEqual(audio.MIC[0], audio.SOUND[0])

    def test_a_hello_is_as_long_as_its_reply_so_answering_amplifies_nothing(self):
        sender, _, _, reply = handshake(token())
        self.assertEqual(len(sender.hello()), len(reply))
        self.assertEqual(len(sender.hello()), audio.HELLO_SIZE)

    def test_a_packet_fits_one_ethernet_frame(self):
        sender, _, _, reply = handshake(token())
        sender.accept_reply(reply)
        self.assertEqual(len(sender.seal(pcm(1))), audio.AUDIO_PACKET_SIZE)
        self.assertLessEqual(audio.AUDIO_PACKET_SIZE, 1472)

    def test_a_machine_that_is_not_paired_gets_no_answer(self):
        sender = audio.SenderSession(token())
        receiver = audio.ReceiverCore({token(): "Other"}, Collector())
        self.assertIsNone(receiver.handle(sender.hello(), 0.0))

    def test_a_reply_made_without_the_token_is_refused(self):
        shared = token()
        sender = audio.SenderSession(shared)
        # A receiver that knows the key id but holds another token: the tag cannot match.
        impostor = audio.ReceiverCore({token(): "Impostor"}, Collector())
        impostor._by_key_id = {protocol.key_id(shared): (token(), "Impostor")}
        reply = impostor.handle(sender.hello(), 0.0)
        self.assertIsNotNone(reply)
        self.assertFalse(sender.accept_reply(reply))
        self.assertFalse(sender.ready)

    def test_a_reply_altered_on_the_way_is_refused(self):
        sender, _, _, reply = handshake(token())
        altered = bytearray(reply)
        altered[-1] ^= 1
        self.assertFalse(sender.accept_reply(bytes(altered)))

    def test_nothing_is_sealed_before_an_answer(self):
        with self.assertRaises(RuntimeError):
            audio.SenderSession(token()).seal(pcm(1))

    def test_a_typed_token_cannot_stream(self):
        with self.assertRaises(ValueError):
            audio.SenderSession("typed by hand")

    def test_a_repeated_hello_gets_the_same_answer(self):
        sender, receiver, _, reply = handshake(token())
        self.assertEqual(receiver.handle(sender.hello(), 1.0), reply)

    def test_a_restarted_receiver_is_followed_with_new_keys(self):
        shared = token()
        sender, receiver, _, reply = handshake(shared)
        sender.accept_reply(reply)
        receiver.handle(sender.seal(pcm(1)), 0.1)
        restarted_buffer = Collector()
        restarted = audio.ReceiverCore({shared: "Studio PC"}, restarted_buffer)
        stale = sender.seal(pcm(2))
        restarted.handle(stale, 0.2)
        self.assertEqual(restarted_buffer.packets, [])
        self.assertTrue(sender.accept_reply(restarted.handle(sender.hello(), 0.3)))
        restarted.handle(sender.seal(pcm(3)), 0.4)
        self.assertEqual(restarted_buffer.packets, ["reset", (1, pcm(3))])

    def test_hellos_are_answered_at_most_twenty_times_a_second(self):
        shared = token()
        receiver = audio.ReceiverCore({shared: "PC"}, Collector())
        answers = [receiver.handle(audio.SenderSession(shared).hello(), 0.5) for _ in range(30)]
        self.assertEqual(sum(answer is not None for answer in answers), audio.REPLIES_PER_SECOND)
        self.assertIsNotNone(receiver.handle(audio.SenderSession(shared).hello(), 1.6))


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.sender, self.receiver, self.buffer, reply = handshake(token())
        self.sender.accept_reply(reply)

    def test_a_tampered_packet_is_dropped(self):
        packet = bytearray(self.sender.seal(pcm(1)))
        packet[audio.AUDIO_HEADER_SIZE + 5] ^= 0x80
        self.receiver.handle(bytes(packet), 0.1)
        self.assertEqual(self.buffer.packets, [])
        self.assertEqual(self.receiver.stats["rejected"], 1)

    def test_a_packet_renumbered_on_the_way_is_dropped(self):
        packet = bytearray(self.sender.seal(pcm(1)))
        packet[audio.AUDIO_HEADER_SIZE - 1] = 9
        self.receiver.handle(bytes(packet), 0.1)
        self.assertEqual(self.buffer.packets, [])

    def test_a_replayed_packet_plays_once(self):
        jitter = audio.JitterBuffer(target=1, limit=4)
        self.receiver.buffer = jitter
        packet = self.sender.seal(pcm(7))
        self.receiver.handle(packet, 0.1)
        self.receiver.handle(packet, 0.2)
        self.assertEqual(jitter.pull(PACKET), pcm(7))
        self.receiver.handle(packet, 0.3)
        self.assertEqual(jitter.pull(PACKET), bytes(PACKET))
        self.assertEqual(jitter.stats["late"], 2)

    def test_rubbish_is_ignored(self):
        for data in (b"", b"BAUD", os.urandom(100), audio.MAGIC + bytes([9, 3]) + bytes(40)):
            self.assertIsNone(self.receiver.handle(data, 0.1))
        self.assertEqual(self.buffer.packets, [])

    def test_the_speakers_are_let_go_after_a_second_of_silence(self):
        changes = []
        self.receiver._on_change = changes.append
        self.receiver.handle(self.sender.seal(pcm(1)), 0.1)
        self.receiver.tick(0.5)
        self.assertEqual(self.receiver.playing, "Studio PC")
        self.receiver.tick(1.2)
        self.assertIsNone(self.receiver.playing)
        self.assertEqual(changes, ["Studio PC", None])

    def test_a_second_sender_waits_for_the_first_to_go_quiet(self):
        other = token()
        self.receiver._by_key_id[protocol.key_id(other)] = (other, "Laptop")
        second = audio.SenderSession(other)
        second.accept_reply(self.receiver.handle(second.hello(), 0.0))
        self.receiver.handle(self.sender.seal(pcm(1)), 0.1)
        self.receiver.handle(second.seal(pcm(2)), 0.2)
        self.assertEqual(self.receiver.playing, "Studio PC")
        self.receiver.handle(second.seal(pcm(3)), 1.5)
        self.assertEqual(self.receiver.playing, "Laptop")


class JitterBufferTests(unittest.TestCase):
    def test_nothing_plays_until_the_target_is_waiting(self):
        buffer = audio.JitterBuffer(target=3, limit=10)
        buffer.push(1, pcm(1))
        buffer.push(2, pcm(2))
        self.assertEqual(buffer.pull(PACKET), bytes(PACKET))
        buffer.push(3, pcm(3))
        self.assertEqual(buffer.pull(PACKET * 3), pcm(1) + pcm(2) + pcm(3))

    def test_packets_out_of_order_play_in_order(self):
        buffer = audio.JitterBuffer(target=3, limit=10)
        for number in (2, 1, 3):
            buffer.push(number, pcm(number))
        self.assertEqual(buffer.pull(PACKET * 3), pcm(1) + pcm(2) + pcm(3))

    def test_a_lost_packet_plays_as_silence_and_a_late_one_is_dropped(self):
        buffer = audio.JitterBuffer(target=2, limit=10)
        buffer.push(1, pcm(1))
        buffer.push(3, pcm(3))
        self.assertEqual(buffer.pull(PACKET * 3), pcm(1) + bytes(PACKET) + pcm(3))
        buffer.push(2, pcm(2))
        self.assertEqual(buffer.stats, {"late": 1, "lost": 1, "underruns": 0, "dropped": 0})

    def test_running_dry_waits_for_the_target_again(self):
        buffer = audio.JitterBuffer(target=2, limit=10)
        buffer.push(1, pcm(1))
        buffer.push(2, pcm(2))
        buffer.pull(PACKET * 2)
        # Emptied exactly: the speakers had all they asked for, so the next packet still plays.
        buffer.push(3, pcm(3))
        self.assertEqual(buffer.pull(PACKET), pcm(3))
        # Asked for more than there is: dry, and silent until two are waiting again.
        self.assertEqual(buffer.pull(PACKET), bytes(PACKET))
        self.assertEqual(buffer.stats["underruns"], 1)
        buffer.push(4, pcm(4))
        self.assertEqual(buffer.pull(PACKET), bytes(PACKET))
        buffer.push(5, pcm(5))
        self.assertEqual(buffer.pull(PACKET * 2), pcm(4) + pcm(5))
        buffer.push(2, pcm(2))
        self.assertEqual(buffer.stats["late"], 1)

    def test_pulls_of_any_size_keep_the_stream_whole(self):
        buffer = audio.JitterBuffer(target=4, limit=10)
        for number in range(1, 5):
            buffer.push(number, pcm(number))
        out = b"".join(buffer.pull(size) for size in (100, 1000, 860, 1880))
        self.assertEqual(out, pcm(1) + pcm(2) + pcm(3) + pcm(4))

    def test_a_sender_running_fast_is_trimmed_back_to_the_target(self):
        buffer = audio.JitterBuffer(target=2, limit=5)
        for number in range(1, 7):
            buffer.push(number, pcm(number))
        self.assertEqual(buffer.depth, 2)
        self.assertEqual(buffer.stats["dropped"], 4)
        self.assertEqual(buffer.pull(PACKET * 2), pcm(5) + pcm(6))


class ChunkerTests(unittest.TestCase):
    def test_whole_packets_come_out_and_the_rest_waits(self):
        chunker = audio.Chunker()
        self.assertEqual(chunker.feed(b"a" * 500), [])
        self.assertEqual(chunker.feed(b"b" * 1500), [b"a" * 500 + b"b" * 460, b"b" * 960])
        self.assertEqual(chunker.feed(b"c" * 880), [b"b" * 80 + b"c" * 880])


class LoopbackTests(unittest.TestCase):
    """The real sockets and threads, over 127.0.0.1."""

    def test_a_tone_crosses_udp_and_plays_in_order(self):
        shared = token()
        buffer = audio.JitterBuffer(target=4, limit=200)
        receiver = audio.AudioReceiver({shared: "PC"}, buffer, port=0, bind="127.0.0.1").start()
        streaming = threading.Event()
        sender = audio.AudioSender(shared, "127.0.0.1", receiver.port,
                                   on_state=lambda up: up and streaming.set()).start()
        try:
            self.assertTrue(streaming.wait(3.0), "the receiver never answered")
            packets = [struct.pack(">I", number) * (PACKET // 4) for number in range(1, 21)]
            for packet in packets:
                sender.send(packet[:300])
                sender.send(packet[300:])
            deadline = time.monotonic() + 2.0
            while buffer.depth < 20 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(buffer.pull(PACKET * 20), b"".join(packets))
            self.assertEqual(receiver.core.playing, "PC")
        finally:
            sender.stop()
            receiver.stop()


if __name__ == "__main__":
    unittest.main()
