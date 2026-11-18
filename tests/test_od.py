import pytest

from canopen_sim import od
from canopen_sim.od import DataType, ObjectDictionary, SdoAbort, Variable, decode, encode


@pytest.mark.parametrize(
    "dtype,value,raw",
    [
        (DataType.BOOLEAN, True, b"\x01"),
        (DataType.INTEGER8, -2, b"\xfe"),
        (DataType.INTEGER16, -1, b"\xff\xff"),
        (DataType.INTEGER32, -2, b"\xfe\xff\xff\xff"),
        (DataType.UNSIGNED8, 200, b"\xc8"),
        (DataType.UNSIGNED16, 0x1234, b"\x34\x12"),
        (DataType.UNSIGNED32, 0x12345678, b"\x78\x56\x34\x12"),
        (DataType.REAL32, 1.5, b"\x00\x00\xc0\x3f"),
        (DataType.VISIBLE_STRING, "SIM-IO", b"SIM-IO"),
        (DataType.OCTET_STRING, b"\x01\x02\x03", b"\x01\x02\x03"),
        (DataType.INTEGER64, -1, b"\xff" * 8),
        (DataType.UNSIGNED64, 2**64 - 1, b"\xff" * 8),
    ],
)
def test_encode_decode_roundtrip(dtype, value, raw):
    assert encode(dtype, value) == raw
    assert decode(dtype, raw) == value


def test_all_datatypes_covered():
    assert len(DataType) == 12


def make_od():
    d = ObjectDictionary()
    d.add(Variable(0x2000, 1, "ro", DataType.UNSIGNED8, "ro", 1))
    d.add(Variable(0x2000, 2, "wo", DataType.UNSIGNED8, "wo", 2))
    d.add(Variable(0x2000, 3, "const", DataType.UNSIGNED8, "const", 3))
    d.add(Variable(0x2001, 0, "rw", DataType.UNSIGNED16, "rw", 5, low=2, high=10))
    return d


def code_of(fn, *args):
    with pytest.raises(SdoAbort) as ei:
        fn(*args)
    return ei.value.code


def test_read_write_only_aborts():
    d = make_od()
    assert code_of(d.sdo_read, 0x2000, 2) == od.WRITE_ONLY
    assert code_of(d.sdo_write, 0x2000, 1, b"\x00") == od.READ_ONLY
    assert code_of(d.sdo_write, 0x2000, 3, b"\x00") == od.READ_ONLY
    assert d.sdo_read(0x2000, 1) == b"\x01"
    d.sdo_write(0x2000, 2, b"\x07")
    assert d.get(0x2000, 2) == 7


def test_unknown_object_and_subindex():
    d = make_od()
    assert code_of(d.sdo_read, 0x3000, 0) == od.NO_OBJECT
    assert code_of(d.sdo_write, 0x3000, 0, b"\x00") == od.NO_OBJECT
    assert code_of(d.sdo_read, 0x2000, 9) == od.NO_SUBINDEX
    assert code_of(d.entry, 0x2001, 1) == od.NO_SUBINDEX


def test_write_length_and_range():
    d = make_od()
    assert code_of(d.sdo_write, 0x2001, 0, b"\x05") == od.TOO_SHORT
    assert code_of(d.sdo_write, 0x2001, 0, b"\x05\x00\x00") == od.TOO_LONG
    assert code_of(d.sdo_write, 0x2001, 0, b"\x0b\x00") == od.TOO_HIGH
    assert code_of(d.sdo_write, 0x2001, 0, b"\x01\x00") == od.TOO_LOW
    assert d.get(0x2001, 0) == 5
    d.sdo_write(0x2001, 0, b"\x0a\x00")
    assert d.get(0x2001, 0) == 10


def test_on_write_veto_keeps_old_value():
    d = make_od()
    seen = []

    def veto(v):
        seen.append(v)
        raise SdoAbort(od.DEVICE_STATE)

    d.on_write(0x2001, 0, veto)
    assert code_of(d.sdo_write, 0x2001, 0, b"\x07\x00") == od.DEVICE_STATE
    assert seen == [7]
    assert d.get(0x2001, 0) == 5


def test_on_write_called_on_success_and_entries():
    d = make_od()
    seen = []
    d.on_write(0x2001, 0, seen.append)
    d.sdo_write(0x2001, 0, b"\x06\x00")
    assert seen == [6]
    assert len(list(d.entries())) == 4
    d.set(0x2001, 0, 99)  # internal set: no range check, no callback
    assert d.get(0x2001, 0) == 99 and seen == [6]
