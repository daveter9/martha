"""Pure-Python stand-in for the one function Grott uses from libscrc (a C extension that is
not packaged for Ubuntu and has no wheel for its Python). Same result as libscrc.modbus:
CRC-16/MODBUS (reflected polynomial 0xA001, initial value 0xFFFF) as an int."""


def modbus(data, crc=0xFFFF):
    for byte in bytes(data):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc
