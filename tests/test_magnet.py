"""Info-hash extraction: the join key between magnets and CloudDrive tasks."""

from reeloom.magnet import extract_info_hash

HEX = "c9e15763f722f23e98a29decdfae341b98d53056"


def test_hex_hash_normalized_to_upper() -> None:
    assert extract_info_hash(f"magnet:?xt=urn:btih:{HEX}") == HEX.upper()
    assert extract_info_hash(f"magnet:?xt=urn:btih:{HEX.upper()}") == HEX.upper()


def test_base32_hash_decoded_to_hex() -> None:
    magnet = "magnet:?xt=urn:btih:ZHQVXWHXELZD5GFCTXW57LRUDOMNKMCW"
    result = extract_info_hash(magnet)
    assert result is not None
    assert len(result) == 40
    assert result == result.upper()
    int(result, 16)


def test_extra_parameters_do_not_matter() -> None:
    magnet = f"magnet:?xt=urn:btih:{HEX}&dn=Some+Name&tr=udp%3A%2F%2Ftracker"
    assert extract_info_hash(magnet) == HEX.upper()


def test_v2_only_magnet_is_rejected() -> None:
    magnet = (
        "magnet:?xt=urn:btmh:1220caf1e1c30e81cb361b9ee167c4aa64228a7fa4fa9f6105232b28ad099f3a302e"
    )
    assert extract_info_hash(magnet) is None


def test_garbage_is_rejected() -> None:
    assert extract_info_hash("not a magnet") is None
    assert extract_info_hash("magnet:?xt=urn:btih:zzzz") is None
    assert extract_info_hash("magnet:?xt=urn:btih:" + "g" * 40) is None
