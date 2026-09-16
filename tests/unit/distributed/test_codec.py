import numpy as np

from src.codec.spike_codec import MultiModalCodec


def test_numeric_encode():
    codec = MultiModalCodec(dim=16)
    signal = codec.encode(1.5, "numeric")
    assert signal.shape == (16,)
    assert np.linalg.norm(signal) > 0


def test_text_encode_tfidf():
    codec = MultiModalCodec(dim=16)
    signal = codec.encode("market crash fears", "text")
    assert signal.shape == (16,)


def test_decode_actions():
    codec = MultiModalCodec(dim=16)
    pattern = np.zeros(16)
    pattern[0] = 0.95
    actions = codec.decode(pattern, threshold=0.3)
    assert actions and actions[0]["type"] == "file_write"


def test_stock_encoding():
    codec = MultiModalCodec(dim=16)
    signal = codec.encode_stock_data(price=175.5, change_pct=3.5, volume=45_000_000)
    assert np.count_nonzero(signal) > 0
