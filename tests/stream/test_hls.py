import os
import typing
import unittest
from threading import Event
from unittest.mock import Mock, call, patch

import pytest
import requests_mock
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from requests.exceptions import InvalidSchema

from streamlink.session import Streamlink
from streamlink.stream.hls import HLSStream, HLSStreamReader, MuxedHLSStream
from streamlink.stream.hls_playlist import M3U8Parser
from tests.mixins.stream_hls import EventedHLSStreamWriter, Playlist, Segment, Tag, TestMixinStreamHLS
from tests.resources import text


class EncryptedBase:
    content: bytes
    content_plain: bytes

    def __init__(self, num, key, iv, *args, content=None, padding=b"", append=b"", **kwargs):
        super().__init__(num, *args, **kwargs)
        aesCipher = AES.new(key, AES.MODE_CBC, iv)
        content = self.content if content is None else content
        padded = content + padding if padding else pad(content, AES.block_size, style="pkcs7")
        self.content_plain = content
        self.content = aesCipher.encrypt(padded) + append


class TagMap(Tag):
    def __init__(self, num, namespace, attrs=None):
        self.path = f"map{num}"
        self.content = f"[map{num}]".encode("ascii")
        super().__init__("EXT-X-MAP", {
            "URI": self.val_quoted_string(self.url(namespace)),
            **(attrs or {}),
        })


class TagMapEnc(EncryptedBase, TagMap):
    pass


class TagKey(Tag):
    path = "encryption.key"

    def __init__(self, method="NONE", uri=None, iv=None, keyformat=None, keyformatversions=None):
        attrs = {"METHOD": method}
        if uri is not False:  # pragma: no branch
            attrs["URI"] = lambda tag, namespace: tag.val_quoted_string(tag.url(namespace))
        if iv is not None:  # pragma: no branch
            attrs["IV"] = self.val_hex(iv)
        if keyformat is not None:  # pragma: no branch
            attrs["KEYFORMAT"] = self.val_quoted_string(keyformat)
        if keyformatversions is not None:  # pragma: no branch
            attrs["KEYFORMATVERSIONS"] = self.val_quoted_string(keyformatversions)
        super().__init__("EXT-X-KEY", attrs)
        self.uri = uri

    def url(self, namespace):
        return self.uri.format(namespace=namespace) if self.uri else super().url(namespace)


class SegmentEnc(EncryptedBase, Segment):
    pass


class TestHLSStreamRepr(unittest.TestCase):
    def test_repr(self):
        session = Streamlink()

        stream = HLSStream(session, "https://foo.bar/playlist.m3u8")
        assert repr(stream) == "<HLSStream ['hls', 'https://foo.bar/playlist.m3u8']>"

        stream = HLSStream(session, "https://foo.bar/playlist.m3u8", "https://foo.bar/master.m3u8")
        assert repr(stream) == "<HLSStream ['hls', 'https://foo.bar/playlist.m3u8', 'https://foo.bar/master.m3u8']>"


class TestHLSVariantPlaylist(unittest.TestCase):
    @classmethod
    def get_master_playlist(cls, playlist):
        with text(playlist) as pl:
            return pl.read()

    def subject(self, playlist, options=None):
        with requests_mock.Mocker() as mock:
            url = f"http://mocked/{self.id()}/master.m3u8"
            content = self.get_master_playlist(playlist)
            mock.get(url, text=content)

            session = Streamlink(options)

            return HLSStream.parse_variant_playlist(session, url)

    def test_variant_playlist(self):
        streams = self.subject("hls/test_master.m3u8")
        assert list(streams.keys()) == ["720p", "720p_alt", "480p", "360p", "160p", "1080p (source)", "90k"]
        assert all(isinstance(stream, HLSStream) for stream in streams.values())
        assert all(stream.multivariant is not None and stream.multivariant.is_master for stream in streams.values())

        base = f"http://mocked/{self.id()}"
        stream = next(iter(streams.values()))
        assert repr(stream) == f"<HLSStream ['hls', '{base}/720p.m3u8', '{base}/master.m3u8']>"

        assert stream.multivariant is not None
        assert stream.multivariant.uri == f"{base}/master.m3u8"
        assert stream.url_master == f"{base}/master.m3u8"

    def test_url_master(self):
        session = Streamlink()
        stream = HLSStream(session, "http://mocked/foo", url_master="http://mocked/master.m3u8")

        assert stream.multivariant is None
        assert stream.url == "http://mocked/foo"
        assert stream.url_master == "http://mocked/master.m3u8"


class EventedHLSReader(HLSStreamReader):
    __writer__ = EventedHLSStreamWriter


class EventedHLSStream(HLSStream):
    __reader__ = EventedHLSReader


@patch("streamlink.stream.hls.HLSStreamWorker.wait", Mock(return_value=True))
class TestHLSStream(TestMixinStreamHLS, unittest.TestCase):
    def get_session(self, options=None, *args, **kwargs):
        session = super().get_session(options)
        session.set_option("hls-live-edge", 3)

        return session

    def test_playlist_end(self):
        thread, segments = self.subject([
            # media sequence = 0
            Playlist(0, [Segment(0)], end=True),
        ])

        assert self.await_read(read_all=True) == self.content(segments), "Stream ends and read-all handshake doesn't time out"

    def test_offset_and_duration(self):
        thread, segments = self.subject([
            Playlist(1234, [Segment(0), Segment(1, duration=0.5), Segment(2, duration=0.5), Segment(3)], end=True),
        ], streamoptions={"start_offset": 1, "duration": 1})

        data = self.await_read(read_all=True)
        assert data == self.content(segments, cond=lambda s: 0 < s.num < 3), "Respects the offset and duration"
        assert all(self.called(s) for s in segments.values() if 0 < s.num < 3), "Downloads second and third segment"
        assert not any(self.called(s) for s in segments.values() if 0 > s.num > 3), "Skips other segments"

    def test_map(self):
        discontinuity = Tag("EXT-X-DISCONTINUITY")
        map1 = TagMap(1, self.id())
        map2 = TagMap(2, self.id())
        self.mock("GET", self.url(map1), content=map1.content)
        self.mock("GET", self.url(map2), content=map2.content)

        thread, segments = self.subject([
            Playlist(0, [map1, Segment(0), Segment(1), Segment(2), Segment(3)]),
            Playlist(4, [map1, Segment(4), map2, Segment(5), Segment(6), discontinuity, Segment(7)], end=True),
        ])

        data = self.await_read(read_all=True, timeout=None)
        assert data == self.content([
            map1, segments[1], map1, segments[2], map1, segments[3],
            map1, segments[4], map2, segments[5], map2, segments[6], segments[7],
        ])
        assert self.called(map1, once=True), "Downloads first map only once"
        assert self.called(map2, once=True), "Downloads second map only once"


@patch("streamlink.stream.hls.HLSStreamWorker.wait", Mock(return_value=True))
class TestHLSStreamByterange(TestMixinStreamHLS, unittest.TestCase):
    __stream__ = EventedHLSStream

    # The dummy segments in the error tests are required because the writer's run loop would otherwise continue forever
    # due to the segment's future result being None (no requests result), and we can't await the end of the stream
    # without waiting for the stream's timeout error. The dummy segments ensure that we can call await_write for these
    # successful segments, so we can close the stream afterwards and safely make the test assertions.
    # The EventedHLSStreamWriter could also implement await_fetch, but this is unnecessarily more complex than it already is.

    @patch("streamlink.stream.hls.log")
    def test_unknown_offset(self, mock_log: Mock):
        thread, _ = self.subject([
            Playlist(0, [
                Tag("EXT-X-BYTERANGE", "3"), Segment(0),
                Segment(1),
            ], end=True),
        ])

        self.await_write(2 - 1)
        self.thread.close()

        assert mock_log.error.call_args_list == [
            call("Failed to fetch segment 0: Missing BYTERANGE offset"),
        ]
        assert not self.called(Segment(0))

    @patch("streamlink.stream.hls.log")
    def test_unknown_offset_map(self, mock_log: Mock):
        map1 = TagMap(1, self.id(), {"BYTERANGE": "\"1234\""})
        self.mock("GET", self.url(map1), content=map1.content)
        thread, _ = self.subject([
            Playlist(0, [
                Segment(0),
                map1,
                Segment(1),
            ], end=True),
        ])

        self.await_write(3 - 1)
        self.thread.close()

        assert mock_log.error.call_args_list == [
            call("Failed to fetch map for segment 1: Missing BYTERANGE offset"),
        ]
        assert not self.called(map1)

    @patch("streamlink.stream.hls.log")
    def test_invalid_offset_reference(self, mock_log: Mock):
        thread, _ = self.subject([
            Playlist(0, [
                Tag("EXT-X-BYTERANGE", "3@0"), Segment(0),
                Segment(1),
                Tag("EXT-X-BYTERANGE", "5"), Segment(2),
                Segment(3),
            ], end=True),
        ])

        self.await_write(4 - 1)
        self.thread.close()

        assert mock_log.error.call_args_list == [
            call("Failed to fetch segment 2: Missing BYTERANGE offset"),
        ]
        assert self.mocks[self.url(Segment(0))].last_request._request.headers["Range"] == "bytes=0-2"
        assert not self.called(Segment(2))

    def test_offsets(self):
        map1 = TagMap(1, self.id(), {"BYTERANGE": "\"1234@0\""})
        map2 = TagMap(2, self.id(), {"BYTERANGE": "\"42@1337\""})
        self.mock("GET", self.url(map1), content=map1.content)
        self.mock("GET", self.url(map2), content=map2.content)
        s1, s2, s3, s4, s5 = Segment(0), Segment(1), Segment(2), Segment(3), Segment(4)

        self.subject([
            Playlist(0, [
                map1,
                Tag("EXT-X-BYTERANGE", "5@3"), s1,
                Tag("EXT-X-BYTERANGE", "7"), s2,
                map2,
                Tag("EXT-X-BYTERANGE", "11"), s3,
                Tag("EXT-X-BYTERANGE", "17@13"), s4,
                Tag("EXT-X-BYTERANGE", "19"), s5,
            ], end=True),
        ])

        self.await_write(5 * 2)
        self.await_read(read_all=True)
        assert self.mocks[self.url(map1)].last_request._request.headers["Range"] == "bytes=0-1233"
        assert self.mocks[self.url(map2)].last_request._request.headers["Range"] == "bytes=1337-1378"
        assert self.mocks[self.url(s1)].last_request._request.headers["Range"] == "bytes=3-7"
        assert self.mocks[self.url(s2)].last_request._request.headers["Range"] == "bytes=8-14"
        assert self.mocks[self.url(s3)].last_request._request.headers["Range"] == "bytes=15-25"
        assert self.mocks[self.url(s4)].last_request._request.headers["Range"] == "bytes=13-29"
        assert self.mocks[self.url(s5)].last_request._request.headers["Range"] == "bytes=30-48"


@patch("streamlink.stream.hls.HLSStreamWorker.wait", Mock(return_value=True))
class TestHLSStreamEncrypted(TestMixinStreamHLS, unittest.TestCase):
    __stream__ = EventedHLSStream

    def get_session(self, options=None, *args, **kwargs):
        session = super().get_session(options)
        session.set_option("hls-live-edge", 3)
        session.set_option("http-headers", {"X-FOO": "BAR"})

        return session

    def gen_key(
        self,
        aes_key=None,
        aes_iv=None,
        method="AES-128",
        uri=None,
        keyformat="identity",
        keyformatversions=1,
        mock=None,
    ):
        aes_key = aes_key or os.urandom(16)
        aes_iv = aes_iv or os.urandom(16)

        key = TagKey(method=method, uri=uri, iv=aes_iv, keyformat=keyformat, keyformatversions=keyformatversions)
        self.mock("GET", key.url(self.id()), **(mock if mock else {"content": aes_key}))

        return aes_key, aes_iv, key

    @patch("streamlink.stream.hls.log")
    def test_hls_encrypted_invalid_method(self, mock_log: Mock):
        aesKey, aesIv, key = self.gen_key(method="INVALID")

        self.subject([
            Playlist(0, [key, SegmentEnc(1, aesKey, aesIv)], end=True),
        ])
        self.await_write()

        self.thread.close()
        self.await_close()

        assert b"".join(self.thread.data) == b""
        assert mock_log.error.mock_calls == [
            call("Failed to create decryptor: Unable to decrypt cipher INVALID"),
        ]

    @patch("streamlink.stream.hls.log")
    def test_hls_encrypted_missing_uri(self, mock_log: Mock):
        aesKey, aesIv, key = self.gen_key(uri=False)

        self.subject([
            Playlist(0, [key, SegmentEnc(1, aesKey, aesIv)], end=True),
        ])
        self.await_write()

        self.thread.close()
        self.await_close()

        assert b"".join(self.thread.data) == b""
        assert mock_log.error.mock_calls == [
            call("Failed to create decryptor: Missing URI for decryption key"),
        ]

    @patch("streamlink.stream.hls.log")
    def test_hls_encrypted_missing_adapter(self, mock_log: Mock):
        aesKey, aesIv, key = self.gen_key(uri="foo://bar/baz", mock={"exc": InvalidSchema})

        self.subject([
            Playlist(0, [key, SegmentEnc(1, aesKey, aesIv)], end=True),
        ])
        self.await_write()

        self.thread.close()
        self.await_close()

        assert b"".join(self.thread.data) == b""
        assert mock_log.error.mock_calls == [
            call("Failed to create decryptor: Unable to find connection adapter for key URI: foo://bar/baz"),
        ]

    def test_hls_encrypted_aes128(self):
        aesKey, aesIv, key = self.gen_key()
        long = b"Test cipher block chaining mode by using a long bytes string"

        # noinspection PyTypeChecker
        thread, segments = self.subject([
            Playlist(0, [key] + [SegmentEnc(num, aesKey, aesIv) for num in range(0, 4)]),
            Playlist(4, [key] + [SegmentEnc(num, aesKey, aesIv, content=long) for num in range(4, 8)], end=True),
        ])

        self.await_write(3 + 4)
        data = self.await_read(read_all=True)
        self.await_close()

        expected = self.content(segments, prop="content_plain", cond=lambda s: s.num >= 1)
        assert data == expected, "Decrypts the AES-128 identity stream"
        assert self.called(key, once=True), "Downloads encryption key only once"
        assert self.get_mock(key).last_request._request.headers.get("X-FOO") == "BAR"
        assert not any(self.called(s) for s in segments.values() if s.num < 1), "Skips first segment"
        assert all(self.called(s) for s in segments.values() if s.num >= 1), "Downloads all remaining segments"
        assert self.get_mock(segments[1]).last_request._request.headers.get("X-FOO") == "BAR"

    def test_hls_encrypted_aes128_with_map(self):
        aesKey, aesIv, key = self.gen_key()
        map1 = TagMapEnc(1, namespace=self.id(), key=aesKey, iv=aesIv)
        map2 = TagMapEnc(2, namespace=self.id(), key=aesKey, iv=aesIv)
        self.mock("GET", self.url(map1), content=map1.content)
        self.mock("GET", self.url(map2), content=map2.content)

        # noinspection PyTypeChecker
        thread, segments = self.subject([
            Playlist(0, [key, map1] + [SegmentEnc(num, aesKey, aesIv) for num in range(0, 2)]),
            Playlist(2, [key, map2] + [SegmentEnc(num, aesKey, aesIv) for num in range(2, 4)], end=True),
        ])

        self.await_write(2 * 2 + 2 * 2)
        data = self.await_read(read_all=True)
        self.await_close()

        assert data == self.content([
            map1, segments[0], map1, segments[1], map2, segments[2], map2, segments[3],
        ], prop="content_plain")

    def test_hls_encrypted_aes128_key_uri_override(self):
        aesKey, aesIv, key = self.gen_key(uri="http://real-mocked/{namespace}/encryption.key?foo=bar")
        aesKeyInvalid = bytes(ord(aesKey[i:i + 1]) ^ 0xFF for i in range(16))
        _, __, key_invalid = self.gen_key(aesKeyInvalid, aesIv, uri="http://mocked/{namespace}/encryption.key?foo=bar")

        # noinspection PyTypeChecker
        thread, segments = self.subject([
            Playlist(0, [key_invalid] + [SegmentEnc(num, aesKey, aesIv) for num in range(0, 4)]),
            Playlist(4, [key_invalid] + [SegmentEnc(num, aesKey, aesIv) for num in range(4, 8)], end=True),
        ], options={"hls-segment-key-uri": "{scheme}://real-{netloc}{path}?{query}"})

        self.await_write(3 + 4)
        data = self.await_read(read_all=True)
        self.await_close()

        expected = self.content(segments, prop="content_plain", cond=lambda s: s.num >= 1)
        assert data == expected, "Decrypts stream from custom key"
        assert not self.called(key_invalid), "Skips encryption key"
        assert self.called(key, once=True), "Downloads custom encryption key"
        assert self.get_mock(key).last_request._request.headers.get("X-FOO") == "BAR"

    @patch("streamlink.stream.hls.log")
    def test_hls_encrypted_aes128_incorrect_block_length(self, mock_log: Mock):
        aesKey, aesIv, key = self.gen_key()

        thread, segments = self.subject([
            Playlist(0, [
                key,
                SegmentEnc(0, aesKey, aesIv, append=b"?"),
                SegmentEnc(1, aesKey, aesIv),
            ], end=True),
        ])
        self.await_write()
        assert thread.reader.writer.is_alive()

        self.await_write()
        data = self.await_read(read_all=True)
        self.await_close()

        assert data == self.content([segments[1]], prop="content_plain")
        assert mock_log.error.mock_calls == [
            call("Error while decrypting segment 0: Data must be padded to 16 byte boundary in CBC mode"),
        ]

    @patch("streamlink.stream.hls.log")
    def test_hls_encrypted_aes128_incorrect_padding_length(self, mock_log: Mock):
        aesKey, aesIv, key = self.gen_key()

        padding = b"\x00" * (AES.block_size - len(b"[0]"))
        thread, segments = self.subject([
            Playlist(0, [
                key,
                SegmentEnc(0, aesKey, aesIv, padding=padding),
                SegmentEnc(1, aesKey, aesIv),
            ], end=True),
        ])
        self.await_write()
        assert thread.reader.writer.is_alive()

        self.await_write()
        data = self.await_read(read_all=True)
        self.await_close()

        assert data == self.content([segments[1]], prop="content_plain")
        assert mock_log.error.mock_calls == [call("Error while decrypting segment 0: Padding is incorrect.")]

    @patch("streamlink.stream.hls.log")
    def test_hls_encrypted_aes128_incorrect_padding_content(self, mock_log: Mock):
        aesKey, aesIv, key = self.gen_key()

        padding = (b"\x00" * (AES.block_size - len(b"[0]") - 1)) + bytes([AES.block_size])
        thread, segments = self.subject([
            Playlist(0, [
                key,
                SegmentEnc(0, aesKey, aesIv, padding=padding),
                SegmentEnc(1, aesKey, aesIv),
            ], end=True),
        ])
        self.await_write()
        assert thread.reader.writer.is_alive()

        self.await_write()
        data = self.await_read(read_all=True)
        self.await_close()

        assert data == self.content([segments[1]], prop="content_plain")
        assert mock_log.error.mock_calls == [call("Error while decrypting segment 0: PKCS#7 padding is incorrect.")]


@patch("streamlink.stream.hls.HLSStreamWorker.wait", Mock(return_value=True))
class TestHlsPlaylistReloadTime(TestMixinStreamHLS, unittest.TestCase):
    segments = [
        Segment(0, duration=11),
        Segment(1, duration=7),
        Segment(2, duration=5),
        Segment(3, duration=3),
    ]

    def get_session(self, options=None, reload_time=None, *args, **kwargs):
        return super().get_session(dict(options or {}, **{
            "hls-live-edge": 3,
            "hls-playlist-reload-time": reload_time,
        }))

    def subject(self, *args, **kwargs):
        thread, segments = super().subject(*args, start=False, **kwargs)

        # mock the worker thread's _playlist_reload_time method, so that the main thread can wait on its call
        playlist_reload_time_called = Event()
        orig_playlist_reload_time = thread.reader.worker._playlist_reload_time

        def mocked_playlist_reload_time(*args, **kwargs):
            playlist_reload_time_called.set()
            return orig_playlist_reload_time(*args, **kwargs)

        # immediately kill the writer thread as we don't need it and don't want to wait for its queue polling to end
        def mocked_futures_get():
            return None, None

        with patch.object(thread.reader.worker, "_playlist_reload_time", side_effect=mocked_playlist_reload_time), \
             patch.object(thread.reader.writer, "_futures_get", side_effect=mocked_futures_get):
            self.start()

            if not playlist_reload_time_called.wait(timeout=5):  # pragma: no cover
                raise RuntimeError("Missing _playlist_reload_time() call")

            # wait for the worker thread to terminate, so that deterministic assertions can be done about the reload time
            thread.reader.worker.join()

            return thread.reader.worker.playlist_reload_time

    def test_hls_playlist_reload_time_default(self):
        time = self.subject([Playlist(0, self.segments, end=True, targetduration=4)], reload_time="default")
        assert time == 4, "default sets the reload time to the playlist's target duration"

    def test_hls_playlist_reload_time_segment(self):
        time = self.subject([Playlist(0, self.segments, end=True, targetduration=4)], reload_time="segment")
        assert time == 3, "segment sets the reload time to the playlist's last segment"

    def test_hls_playlist_reload_time_segment_no_segments(self):
        time = self.subject([Playlist(0, [], end=True, targetduration=4)], reload_time="segment")
        assert time == 4, "segment sets the reload time to the targetduration if no segments are available"

    def test_hls_playlist_reload_time_segment_no_segments_no_targetduration(self):
        time = self.subject([Playlist(0, [], end=True, targetduration=0)], reload_time="segment")
        assert time == 6, "sets reload time to 6 seconds when no segments and no targetduration are available"

    def test_hls_playlist_reload_time_live_edge(self):
        time = self.subject([Playlist(0, self.segments, end=True, targetduration=4)], reload_time="live-edge")
        assert time == 8, "live-edge sets the reload time to the sum of the number of segments of the live-edge"

    def test_hls_playlist_reload_time_live_edge_no_segments(self):
        time = self.subject([Playlist(0, [], end=True, targetduration=4)], reload_time="live-edge")
        assert time == 4, "live-edge sets the reload time to the targetduration if no segments are available"

    def test_hls_playlist_reload_time_live_edge_no_segments_no_targetduration(self):
        time = self.subject([Playlist(0, [], end=True, targetduration=0)], reload_time="live-edge")
        assert time == 6, "sets reload time to 6 seconds when no segments and no targetduration are available"

    def test_hls_playlist_reload_time_number(self):
        time = self.subject([Playlist(0, self.segments, end=True, targetduration=4)], reload_time="2")
        assert time == 2, "number values override the reload time"

    def test_hls_playlist_reload_time_number_invalid(self):
        time = self.subject([Playlist(0, self.segments, end=True, targetduration=4)], reload_time="0")
        assert time == 4, "invalid number values set the reload time to the playlist's targetduration"

    def test_hls_playlist_reload_time_no_target_duration(self):
        time = self.subject([Playlist(0, self.segments, end=True, targetduration=0)], reload_time="default")
        assert time == 8, "uses the live-edge sum if the playlist is missing the targetduration data"

    def test_hls_playlist_reload_time_no_data(self):
        time = self.subject([Playlist(0, [], end=True, targetduration=0)], reload_time="default")
        assert time == 6, "sets reload time to 6 seconds when no data is available"


@patch("streamlink.stream.hls.log")
@patch("streamlink.stream.hls.HLSStreamWorker.wait", Mock(return_value=True))
class TestHlsPlaylistParseErrors(TestMixinStreamHLS, unittest.TestCase):
    __stream__ = EventedHLSStream

    class FakePlaylist(typing.NamedTuple):
        is_master: bool = False
        iframes_only: bool = False

    class InvalidPlaylist(Playlist):
        def build(self, *args, **kwargs):
            return "invalid"

    def test_generic(self, mock_log):
        self.subject([self.InvalidPlaylist()])
        assert self.await_read(read_all=True) == b""
        self.await_close()
        assert self.thread.reader.buffer.closed, "Closes the stream on initial playlist parsing error"
        assert mock_log.debug.mock_calls == [call("Reloading playlist")]
        assert mock_log.error.mock_calls == [call("Missing #EXTM3U header")]

    def test_reload(self, mock_log):
        thread, segments = self.subject([
            Playlist(1, [Segment(0)]),
            self.InvalidPlaylist(),
            self.InvalidPlaylist(),
            Playlist(2, [Segment(2)], end=True),
        ])
        self.await_write(2)
        data = self.await_read(read_all=True)
        assert data == self.content(segments)
        self.close()
        self.await_close()
        assert mock_log.warning.mock_calls == [
            call("Failed to reload playlist: Missing #EXTM3U header"),
            call("Failed to reload playlist: Missing #EXTM3U header"),
        ]

    @patch("streamlink.stream.hls.HLSStreamWorker._reload_playlist", Mock(return_value=FakePlaylist(is_master=True)))
    def test_is_master(self, mock_log):
        self.subject([Playlist()])
        assert self.await_read(read_all=True) == b""
        self.await_close()
        assert self.thread.reader.buffer.closed, "Closes the stream on initial playlist parsing error"
        assert mock_log.debug.mock_calls == [call("Reloading playlist")]
        assert mock_log.error.mock_calls == [
            call(f"Attempted to play a variant playlist, use 'hls://{self.stream.url}' instead"),
        ]

    @patch("streamlink.stream.hls.HLSStreamWorker._reload_playlist", Mock(return_value=FakePlaylist(iframes_only=True)))
    def test_iframes_only(self, mock_log):
        self.subject([Playlist()])
        assert self.await_read(read_all=True) == b""
        self.await_close()
        assert self.thread.reader.buffer.closed, "Closes the stream on initial playlist parsing error"
        assert mock_log.debug.mock_calls == [call("Reloading playlist")]
        assert mock_log.error.mock_calls == [call("Streams containing I-frames only are not playable")]


class TestHlsExtAudio:
    @pytest.fixture(autouse=True)
    def _is_usable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("streamlink.stream.hls.FFMPEGMuxer.is_usable", Mock(return_value=True))

    @pytest.fixture(autouse=True)
    def _playlist(self):
        with text("hls/test_2.m3u8") as playlist, \
             requests_mock.Mocker() as mock_requests:
            mock_requests.get("http://mocked/path/master.m3u8", text=playlist.read())
            yield

    @pytest.fixture()
    def stream(self, session: Streamlink):
        streams = HLSStream.parse_variant_playlist(session, "http://mocked/path/master.m3u8")
        assert "video" in streams

        return streams["video"]

    def test_no_selection(self, stream: HLSStream):
        assert not isinstance(stream, MuxedHLSStream)
        assert stream.url == "http://mocked/path/playlist.m3u8"

    @pytest.mark.parametrize(("session", "selection"), [
        pytest.param({"hls-audio-select": ["en"]}, "http://mocked/path/en.m3u8", id="English"),
        pytest.param({"hls-audio-select": ["es"]}, "http://mocked/path/es.m3u8", id="Spanish"),
    ], indirect=["session"])
    def test_selection(self, session: Streamlink, stream: MuxedHLSStream, selection: str):
        assert isinstance(stream, MuxedHLSStream)
        assert [substream.url for substream in stream.substreams] == [
            "http://mocked/path/playlist.m3u8",
            selection,
        ]

    @pytest.mark.parametrize("session", [
        pytest.param({"hls-audio-select": ["*"]}, id="wildcard"),
        pytest.param({"hls-audio-select": ["en", "es"]}, id="multiple locales"),
    ], indirect=["session"])
    def test_multiple(self, session: Streamlink, stream: MuxedHLSStream):
        assert isinstance(stream, MuxedHLSStream)
        assert [substream.url for substream in stream.substreams] == [
            "http://mocked/path/playlist.m3u8",
            "http://mocked/path/en.m3u8",
            "http://mocked/path/es.m3u8",
        ]


class TestM3U8ParserLogging:
    @pytest.mark.parametrize(("loglevel", "has_logs"), [("trace", False), ("all", True)])
    def test_log(self, caplog: pytest.LogCaptureFixture, loglevel: str, has_logs: bool):
        caplog.set_level(loglevel, "streamlink")

        parser = M3U8Parser()
        with text("hls/test_1.m3u8") as pl:
            data = pl.read()
        parser.parse(data)

        assert bool(caplog.records) is has_logs
