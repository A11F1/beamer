"""An experimental audio stream: one machine's sound played on a paired peer's speakers.

A local proof of concept, not part of wire version 6. It never touches the input link: it runs on
its own UDP port, so a late audio packet can never hold up a keystroke the way it would inside the
link's ordered TCP stream (WIRE.md section 2, frames). Upstream is designing audio of its own
(kalkman-code/beamer#23); this is meant to be thrown away when that lands, so it adds no `caps`
entry and nothing to the settings file.

The stream reuses a pair's token and nothing else. The sender opens it with a `hello` holding the
pair's key id and an ephemeral X25519 share; the receiver answers with its own share and a tag only
a holder of the token could make. Both then key the stream from HKDF-SHA256 over the token and the
X25519 result, as the link does, so a recording of it cannot be read later even by someone who
learns the token. Audio packets are ChaCha20-Poly1305 sealed, numbered from 1, and the receiver
plays each number at most once.

    hello  = MAGIC || VERSION || 0x01 || key_id (16) || share_s (32) || zeros (48)
    reply  = MAGIC || VERSION || 0x02 || key_id (16) || share_s (32) || share_r (32) || tag (16)
    audio  = MAGIC || VERSION || 0x03 || sid (8) || seq (8, big-endian) || ciphertext || tag (16)

    ikm    = token text (43 bytes) || X25519(secret, peer share)
    key    = HKDF-SHA256(ikm, salt, b"audio" || key_id || share_s || share_r)
    check  = HKDF-SHA256(ikm, salt, b"confirm" || key_id || share_s || share_r)
    tag    = ChaCha20-Poly1305(check, nonce = 12 zero bytes, plaintext = empty, aad = reply[:86])
    sid    = SHA-256(share_s || share_r)[:8]
    nonce  = 4 zero bytes || seq (8, big-endian); aad = audio[:22]

Two kinds of stream run this way, each on its own port and keyed under its own salt, so neither's
receiver can answer the other's sender: SOUND (a machine's output, port 24823, salt
"beamer-audio-v1") and MIC (a machine's microphone, port 24824, salt "beamer-mic-v1").

A hello is padded to a reply's length, so answering one sent from a forged address amplifies
nothing. Each audio packet carries 5 ms of 48 kHz, 16-bit, stereo PCM, 960 bytes, uncompressed:
about 1.6 Mbit/s with headers, small beside a LAN and under a 1500-byte MTU, so nothing is split
into IP fragments.
"""

import array
import hashlib
import hmac
import logging
import os
import socket
import struct
import threading
import time

from nacl.bindings import (
    crypto_aead_chacha20poly1305_ietf_decrypt,
    crypto_aead_chacha20poly1305_ietf_encrypt,
    crypto_scalarmult_base,
)
from nacl.exceptions import CryptoError

from core import protocol

LOGGER = logging.getLogger("Beamer")

AUDIO_PORT = 24823
MAGIC = b"BAUD"
VERSION = 1
KIND_HELLO = 1
KIND_REPLY = 2
KIND_AUDIO = 3
AUDIO_SALT = b"beamer-audio-v1"
MIC_PORT = 24824
MIC_SALT = b"beamer-mic-v1"
# Each kind of stream: its port and its salt.
SOUND = (AUDIO_PORT, AUDIO_SALT)
MIC = (MIC_PORT, MIC_SALT)

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES
PACKET_FRAMES = 240
PACKET_BYTES = PACKET_FRAMES * FRAME_BYTES
PACKET_SECONDS = PACKET_FRAMES / SAMPLE_RATE

_HEAD = len(MAGIC) + 2
_KEY_ID = protocol.KEY_ID_SIZE
_SHARE = protocol.SHARE_SIZE
_TAG = 16
SID_SIZE = 8
REPLY_SIZE = _HEAD + _KEY_ID + 2 * _SHARE + _TAG
HELLO_SIZE = REPLY_SIZE
AUDIO_HEADER_SIZE = _HEAD + SID_SIZE + 8
AUDIO_PACKET_SIZE = AUDIO_HEADER_SIZE + PACKET_BYTES + _TAG
_ZERO_NONCE = bytes(12)

# The sender says hello this often while no receiver has answered, and keeps saying it once one
# has, so a receiver that restarted is found again within a couple of seconds.
HELLO_SECONDS = 1.0
KEEPALIVE_SECONDS = 2.0
# A sender stops streaming when its receiver has not answered for this long.
ANSWER_LOST_SECONDS = 6.0
# The receiver forgets a session after this long with nothing from it, keeps at most this many,
# and answers at most this many hellos a second in all.
SESSION_IDLE_SECONDS = 10.0
MAX_SESSIONS = 8
REPLIES_PER_SECOND = 20
# The playing stream keeps the speakers until it has been silent this long.
HOLD_SECONDS = 1.0


def _head(kind):
    return MAGIC + bytes([VERSION, kind])


def _kind(data):
    if len(data) < _HEAD or data[:len(MAGIC)] != MAGIC or data[len(MAGIC)] != VERSION:
        return None
    return data[len(MAGIC) + 1]


def _keys(token, shared, key_id, share_s, share_r, salt=AUDIO_SALT):
    ikm = protocol.link_ikm(token, shared)
    tail = key_id + share_s + share_r
    return (protocol.hkdf_sha256(ikm, salt, b"audio" + tail),
            protocol.hkdf_sha256(ikm, salt, b"confirm" + tail))


def stream_id(share_s, share_r):
    return hashlib.sha256(share_s + share_r).digest()[:SID_SIZE]


def _reply_tag(check_key, reply_head):
    return crypto_aead_chacha20poly1305_ietf_encrypt(b"", reply_head, _ZERO_NONCE, check_key)


def _audio_nonce(seq):
    return bytes(4) + struct.pack(">Q", seq)


class SenderSession:
    """The sending half of one stream, with no socket: `hello()` to send, `accept_reply()` for each
    answer, `seal()` for each 960-byte packet of PCM. Not thread-safe; AudioSender locks it."""

    def __init__(self, token, secret=None, salt=AUDIO_SALT):
        if not protocol.is_paired_token(token):
            raise ValueError("audio needs a token made by pairing")
        self._token = token
        self._salt = salt
        self.key_id = protocol.key_id(token)
        self._secret = os.urandom(_SHARE) if secret is None else bytes(secret)
        self.share = crypto_scalarmult_base(self._secret)
        self.peer_share = None
        self.sid = None
        self._key = None
        self._seq = 0

    @property
    def ready(self):
        return self._key is not None

    def hello(self):
        packet = _head(KIND_HELLO) + self.key_id + self.share
        return packet + bytes(HELLO_SIZE - len(packet))

    def accept_reply(self, data):
        """True when `data` is a genuine answer to this session's hello. A receiver that restarted
        answers with a new share, and the stream moves to its keys with the numbering from 1."""
        if len(data) != REPLY_SIZE or _kind(data) != KIND_REPLY:
            return False
        at = _HEAD
        key_id, share_s = data[at:at + _KEY_ID], data[at + _KEY_ID:at + _KEY_ID + _SHARE]
        share_r = data[at + _KEY_ID + _SHARE:at + _KEY_ID + 2 * _SHARE]
        if key_id != self.key_id or share_s != self.share:
            return False
        if share_r == self.peer_share:
            return True
        try:
            shared = protocol._x25519_shared(self._secret, share_r)
        except protocol.PreambleRefused:
            return False
        key, check = _keys(self._token, shared, self.key_id, self.share, share_r, self._salt)
        tag = data[-_TAG:]
        if not _same(tag, _reply_tag(check, data[:-_TAG])):
            return False
        self.peer_share, self._key, self.sid, self._seq = share_r, key, stream_id(self.share, share_r), 0
        return True

    def seal(self, pcm):
        if self._key is None:
            raise RuntimeError("no receiver has answered yet")
        self._seq += 1
        head = _head(KIND_AUDIO) + self.sid + struct.pack(">Q", self._seq)
        return head + crypto_aead_chacha20poly1305_ietf_encrypt(bytes(pcm), head, _audio_nonce(self._seq), self._key)


def _same(a, b):
    return hmac.compare_digest(a, b)


class _Stream:
    def __init__(self, sid, key, peer, share_s, reply, now):
        self.sid, self.key, self.peer, self.share_s, self.reply = sid, key, peer, share_s, reply
        self.last_seen = now
        self.last_audio = None


class ReceiverCore:
    """The receiving half, with no socket: `handle(data, now)` takes each datagram and returns the
    bytes to send back, if any. `peers` maps each paired token to the name to show; a hello under
    any other key id is ignored without an answer. Audio from the stream that has the speakers goes
    into `buffer`; `on_change(name or None)` is called when that stream changes."""

    def __init__(self, peers, buffer, on_change=None, secret_source=os.urandom, salt=AUDIO_SALT):
        self._salt = salt
        self._by_key_id = {}
        for token, name in peers.items():
            if protocol.is_paired_token(token):
                self._by_key_id[protocol.key_id(token)] = (token, name)
        self.buffer = buffer
        self._on_change = on_change
        self._secret_source = secret_source
        self._streams = {}
        self._playing = None
        self._replies = []
        self.stats = {"packets": 0, "rejected": 0, "replies": 0}

    @property
    def playing(self):
        return self._playing.peer if self._playing else None

    def handle(self, data, now):
        kind = _kind(data)
        if kind == KIND_HELLO and len(data) == HELLO_SIZE:
            return self._hello(data, now)
        if kind == KIND_AUDIO and len(data) > AUDIO_HEADER_SIZE + _TAG:
            self._audio(data, now)
        return None

    def _hello(self, data, now):
        key_id = data[_HEAD:_HEAD + _KEY_ID]
        share_s = data[_HEAD + _KEY_ID:_HEAD + _KEY_ID + _SHARE]
        known = self._by_key_id.get(key_id)
        if known is None:
            return None
        self._replies = [stamp for stamp in self._replies if now - stamp < 1.0]
        if len(self._replies) >= REPLIES_PER_SECOND:
            return None
        self._expire(now)
        for stream in self._streams.values():
            if stream.share_s == share_s and stream.reply[_HEAD:_HEAD + _KEY_ID] == key_id:
                stream.last_seen = now
                self._replies.append(now)
                return stream.reply
        token, name = known
        secret = self._secret_source(_SHARE)
        share_r = crypto_scalarmult_base(secret)
        try:
            shared = protocol._x25519_shared(secret, share_s)
        except protocol.PreambleRefused:
            return None
        key, check = _keys(token, shared, key_id, share_s, share_r, self._salt)
        head = _head(KIND_REPLY) + key_id + share_s + share_r
        reply = head + _reply_tag(check, head)
        sid = stream_id(share_s, share_r)
        while len(self._streams) >= MAX_SESSIONS:
            oldest = min(self._streams.values(), key=lambda stream: stream.last_seen)
            self._drop(oldest.sid)
        self._streams[sid] = _Stream(sid, key, name, share_s, reply, now)
        self._replies.append(now)
        self.stats["replies"] += 1
        return reply

    def _audio(self, data, now):
        head = data[:AUDIO_HEADER_SIZE]
        sid = head[_HEAD:_HEAD + SID_SIZE]
        stream = self._streams.get(sid)
        if stream is None:
            self.stats["rejected"] += 1
            return
        (seq,) = struct.unpack(">Q", head[_HEAD + SID_SIZE:])
        try:
            pcm = crypto_aead_chacha20poly1305_ietf_decrypt(data[AUDIO_HEADER_SIZE:], head, _audio_nonce(seq), stream.key)
        except CryptoError:
            self.stats["rejected"] += 1
            return
        if len(pcm) != PACKET_BYTES or seq == 0:
            self.stats["rejected"] += 1
            return
        stream.last_seen = stream.last_audio = now
        playing = self._playing
        if playing is not stream:
            if playing is not None and playing.last_audio is not None and now - playing.last_audio < HOLD_SECONDS:
                return
            self._playing = stream
            self.buffer.reset()
            if self._on_change:
                self._on_change(stream.peer)
        self.stats["packets"] += 1
        self.buffer.push(seq, pcm)

    def tick(self, now):
        """Called now and then: forgets idle streams and lets the speakers go."""
        self._expire(now)
        playing = self._playing
        if playing is not None and (playing.last_audio is None or now - playing.last_audio >= HOLD_SECONDS):
            self._playing = None
            self.buffer.reset()
            if self._on_change:
                self._on_change(None)

    def _expire(self, now):
        for sid in [sid for sid, stream in self._streams.items() if now - stream.last_seen > SESSION_IDLE_SECONDS]:
            self._drop(sid)

    def _drop(self, sid):
        stream = self._streams.pop(sid)
        if stream is self._playing:
            self._playing = None
            self.buffer.reset()
            if self._on_change:
                self._on_change(None)


class JitterBuffer:
    """Packets in, an even stream of PCM out. `push(seq, pcm)` from the network thread, `pull(n)`
    from the audio device's callback, which must never wait long, so the lock is held only to move
    a few packets.

    Playback starts once `target` packets are waiting, and starts again from there after the buffer
    runs dry. A packet missing when its turn comes plays as silence, and one arriving after that is
    dropped as late. Past `limit` packets waiting (the sender's clock running fast against the
    speakers'), the oldest are dropped back to `target`.

    With `fade` (the default), the sound never jumps: a hard cut from a waveform to silence, or
    from silence back into it, is heard as a click. So where the stream breaks (a lost packet, or
    running dry) the last packet played is played again fading out, and the first packet after a
    break fades in."""

    def __init__(self, target=8, limit=40, fade=True):
        if not 1 <= target < limit:
            raise ValueError("need 1 <= target < limit")
        self.target, self.limit, self.fade = target, limit, fade
        self._lock = threading.Lock()
        self.stats = {"late": 0, "lost": 0, "underruns": 0, "dropped": 0}
        self.reset()

    def reset(self):
        with self._lock:
            self._packets = {}
            self._next = None
            self._primed = False
            self._carry = b""
            # The last packet played, to fade out where the stream breaks; and whether the next
            # packet comes after a break, so fades in.
            self._last = None
            self._rise = True

    @property
    def depth(self):
        with self._lock:
            return len(self._packets)

    def push(self, seq, pcm):
        with self._lock:
            # Until the first packet plays, nothing is late: the first to arrive need not be the
            # first sent.
            if (self._next is not None and seq < self._next) or seq in self._packets:
                self.stats["late"] += 1
                return
            self._packets[seq] = pcm
            if len(self._packets) > self.limit:
                keep = sorted(self._packets)[-self.target:]
                self.stats["dropped"] += len(self._packets) - len(keep)
                self._packets = {number: self._packets[number] for number in keep}
                if self._next is not None:
                    self._next = keep[0]

    def pull(self, size):
        out = bytearray()

        def play(pcm):
            room = size - len(out)
            out.extend(pcm[:room])
            self._carry = pcm[room:]

        with self._lock:
            if self._carry:
                out += self._carry[:size]
                self._carry = self._carry[size:]
            while len(out) < size:
                if not self._primed:
                    if len(self._packets) < self.target:
                        break
                    self._primed = True
                    self._next = min(self._packets)
                if not self._packets:
                    self._primed = False
                    self.stats["underruns"] += 1
                    if self.fade and self._last is not None:
                        play(_ramp(self._last, rising=False))
                    self._last, self._rise = None, True
                    break
                pcm = self._packets.pop(self._next, None)
                self._next += 1
                if pcm is None:
                    self.stats["lost"] += 1
                    pcm = _ramp(self._last, rising=False) if self.fade and self._last is not None else bytes(PACKET_BYTES)
                    self._last, self._rise = None, True
                else:
                    if self.fade and self._rise:
                        pcm = _ramp(pcm, rising=True)
                    self._last, self._rise = pcm, False
                play(pcm)
        if len(out) < size:
            out += bytes(size - len(out))
        return bytes(out)


def _ramp(pcm, rising):
    """A packet of 16-bit stereo PCM faded in from silence (`rising`) or out to it."""
    samples = array.array("h", pcm)
    frames = len(samples) // CHANNELS
    for frame in range(frames):
        gain = (frame + 1) / frames if rising else (frames - 1 - frame) / frames
        for at in range(frame * CHANNELS, frame * CHANNELS + CHANNELS):
            samples[at] = int(samples[at] * gain)
    return samples.tobytes()


class Chunker:
    """Cuts PCM of any length into whole 960-byte packets, keeping the remainder for next time."""

    def __init__(self):
        self._held = bytearray()

    def feed(self, pcm):
        self._held += pcm
        whole = len(self._held) - len(self._held) % PACKET_BYTES
        chunks = [bytes(self._held[at:at + PACKET_BYTES]) for at in range(0, whole, PACKET_BYTES)]
        del self._held[:whole]
        return chunks


class AudioSender:
    """Streams PCM to one receiver over UDP. `send(pcm)` takes 48 kHz 16-bit stereo of any length
    from the capture thread and returns at once; nothing goes out until the receiver has answered,
    and audio captured meanwhile is dropped, as it would be stale by then."""

    def __init__(self, token, host, port=AUDIO_PORT, on_state=None, salt=AUDIO_SALT):
        self._session = SenderSession(token, salt=salt)
        self._address = (host, port)
        self._on_state = on_state
        self._lock = threading.Lock()
        self._chunker = Chunker()
        self._stop = threading.Event()
        self._answered_at = None
        self._streaming = False
        self.sent = 0
        # IPv4 only, as AudioReceiver binds: a peer's saved host is an IPv4 address or a name.
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        family, _, _, _, address = infos[0]
        self._sock = socket.socket(family, socket.SOCK_DGRAM)
        self._sock.connect(address)
        self._sock.settimeout(0.2)
        self._threads = [threading.Thread(target=self._listen, name="audio-replies", daemon=True),
                         threading.Thread(target=self._greet, name="audio-hello", daemon=True)]

    def start(self):
        for thread in self._threads:
            thread.start()
        return self

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(1.0)
        self._sock.close()

    @property
    def streaming(self):
        return self._streaming

    def send(self, pcm):
        packets = []
        with self._lock:
            if not self._streaming:
                self._chunker = Chunker()
                return
            for chunk in self._chunker.feed(pcm):
                packets.append(self._session.seal(chunk))
        for packet in packets:
            try:
                self._sock.send(packet)
                self.sent += 1
            except OSError as error:
                LOGGER.debug("audio packet not sent: %s", error)

    def _greet(self):
        while not self._stop.is_set():
            with self._lock:
                hello = self._session.hello()
                answered = self._answered_at
                if self._streaming and (answered is None or time.monotonic() - answered > ANSWER_LOST_SECONDS):
                    self._set_streaming(False)
            try:
                self._sock.send(hello)
            except OSError as error:
                LOGGER.debug("audio hello not sent: %s", error)
            self._stop.wait(KEEPALIVE_SECONDS if self._streaming else HELLO_SECONDS)

    def _listen(self):
        while not self._stop.is_set():
            try:
                data = self._sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                # A refused port (nothing listening yet) arrives here as ECONNREFUSED on a
                # connected UDP socket; the next hello tries again.
                self._stop.wait(0.2)
                continue
            with self._lock:
                if self._session.accept_reply(data):
                    self._answered_at = time.monotonic()
                    if not self._streaming:
                        self._set_streaming(True)

    def _set_streaming(self, value):
        self._streaming = value
        if self._on_state:
            self._on_state(value)


class AudioReceiver:
    """A ReceiverCore on a UDP socket, run on its own thread."""

    def __init__(self, peers, buffer, port=AUDIO_PORT, bind="0.0.0.0", on_change=None, salt=AUDIO_SALT):
        self.core = ReceiverCore(peers, buffer, on_change=on_change, salt=salt)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((bind, port))
        self._sock.settimeout(0.25)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio-receiver", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(1.0)
        self._sock.close()

    def _run(self):
        last_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                data, address = self._sock.recvfrom(2048)
            except socket.timeout:
                data = None
            except OSError:
                if self._stop.is_set():
                    return
                continue
            now = time.monotonic()
            if data:
                reply = self.core.handle(data, now)
                if reply:
                    try:
                        self._sock.sendto(reply, address)
                    except OSError as error:
                        LOGGER.debug("audio reply not sent: %s", error)
            if now - last_tick >= 0.25:
                self.core.tick(now)
                last_tick = now
