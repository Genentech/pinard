package engram

// PortForVignoble returns the deterministic engram port for the given vignoble
// name (already stripped of the "vignoble-" prefix). Mirrors the formula in
// bin/pinard:
//
//	printf '%s' name | cksum | awk '{print $1}') % 1000 + 7500
//
// Uses the POSIX cksum CRC-32 algorithm (polynomial 0x04C11DB7, unreflected,
// length-appended, XOR-inverted at the end) so Go and bash agree on the port.
func PortForVignoble(name string) int {
	return int(posixCksum([]byte(name)))%1000 + 7500
}

// posixCksum computes the POSIX cksum(1) CRC for data, matching the output of
// `printf '%s' <data> | cksum | awk '{print $1}'` on Linux (GNU coreutils).
//
// Algorithm per POSIX:
//   - Process each byte through a CRC table built from polynomial 0x04C11DB7.
//   - Append the byte-length of the input (low-order byte first, until zero).
//   - XOR-invert the final CRC.
func posixCksum(data []byte) uint32 {
	crc := uint32(0)
	for _, b := range data {
		crc = (crc << 8) ^ crcPOSIXTable[byte(crc>>24)^b]
	}
	n := len(data)
	for n != 0 {
		crc = (crc << 8) ^ crcPOSIXTable[byte(crc>>24)^byte(n&0xFF)]
		n >>= 8
	}
	return ^crc
}

// crcPOSIXTable is the lookup table for the POSIX CRC-32 algorithm.
var crcPOSIXTable = func() [256]uint32 {
	var t [256]uint32
	for i := range t {
		crc := uint32(i) << 24
		for j := 0; j < 8; j++ {
			if crc&0x80000000 != 0 {
				crc = (crc << 1) ^ 0x04C11DB7
			} else {
				crc <<= 1
			}
		}
		t[i] = crc
	}
	return t
}()
