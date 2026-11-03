// PearlMiningV2 proof search: keccak256(seed[32] || nonce[32, big-endian]) with the top 30 bits zero.
// nonce = prefix (24 random bytes, fixed per run) || counter (8 bytes, big-endian).
// Lanes are little-endian 64-bit words of the 136-byte Keccak-256 rate block.

#define ROL(x, n) rotate((ulong)(x), (ulong)(n))

__constant ulong RC[24] = {
  0x0000000000000001UL, 0x0000000000008082UL, 0x800000000000808aUL, 0x8000000080008000UL,
  0x000000000000808bUL, 0x0000000080000001UL, 0x8000000080008081UL, 0x8000000000008009UL,
  0x000000000000008aUL, 0x0000000000000088UL, 0x0000000080008009UL, 0x000000008000000aUL,
  0x000000008000808bUL, 0x800000000000008bUL, 0x8000000000008089UL, 0x8000000000008003UL,
  0x8000000000008002UL, 0x8000000000000080UL, 0x000000000000800aUL, 0x800000008000000aUL,
  0x8000000080008081UL, 0x8000000000008080UL, 0x0000000080000001UL, 0x8000000080008008UL
};

inline ulong bswap64(ulong x) {
  return ((x & 0x00000000000000ffUL) << 56) | ((x & 0x000000000000ff00UL) << 40) |
         ((x & 0x0000000000ff0000UL) << 24) | ((x & 0x00000000ff000000UL) << 8)  |
         ((x & 0x000000ff00000000UL) >> 8)  | ((x & 0x0000ff0000000000UL) >> 24) |
         ((x & 0x00ff000000000000UL) >> 40) | ((x & 0xff00000000000000UL) >> 56);
}

__kernel void search(ulong s0, ulong s1, ulong s2, ulong s3,
                     ulong p4, ulong p5, ulong p6,
                     ulong base, uint iters, ulong mask,
                     __global volatile uint *found_count,
                     __global ulong *found)
{
  ulong gid = get_global_id(0);
  ulong gsz = get_global_size(0);
  for (uint it = 0; it < iters; ++it) {
    ulong ctr = base + (ulong)it * gsz + gid;
    ulong a00 = s0, a01 = s1, a02 = s2, a03 = s3, a04 = p4;
    ulong a05 = p5, a06 = p6, a07 = bswap64(ctr), a08 = 0x01UL, a09 = 0;
    ulong a10 = 0, a11 = 0, a12 = 0, a13 = 0, a14 = 0;
    ulong a15 = 0, a16 = 0x8000000000000000UL, a17 = 0, a18 = 0, a19 = 0;
    ulong a20 = 0, a21 = 0, a22 = 0, a23 = 0, a24 = 0;
    #pragma unroll
    for (int r = 0; r < 24; ++r) {
      ulong c0 = a00 ^ a05 ^ a10 ^ a15 ^ a20;
      ulong c1 = a01 ^ a06 ^ a11 ^ a16 ^ a21;
      ulong c2 = a02 ^ a07 ^ a12 ^ a17 ^ a22;
      ulong c3 = a03 ^ a08 ^ a13 ^ a18 ^ a23;
      ulong c4 = a04 ^ a09 ^ a14 ^ a19 ^ a24;
      ulong d0 = c4 ^ ROL(c1, 1);
      ulong d1 = c0 ^ ROL(c2, 1);
      ulong d2 = c1 ^ ROL(c3, 1);
      ulong d3 = c2 ^ ROL(c4, 1);
      ulong d4 = c3 ^ ROL(c0, 1);
      // theta + rho + pi: b[y][2x+3y] = rot(a[x][y] ^ d[x], r[x][y])
      ulong b00 = a00 ^ d0;
      ulong b10 = ROL(a01 ^ d1, 1);
      ulong b20 = ROL(a02 ^ d2, 62);
      ulong b05 = ROL(a03 ^ d3, 28);
      ulong b15 = ROL(a04 ^ d4, 27);
      ulong b16 = ROL(a05 ^ d0, 36);
      ulong b01 = ROL(a06 ^ d1, 44);
      ulong b11 = ROL(a07 ^ d2, 6);
      ulong b21 = ROL(a08 ^ d3, 55);
      ulong b06 = ROL(a09 ^ d4, 20);
      ulong b07 = ROL(a10 ^ d0, 3);
      ulong b17 = ROL(a11 ^ d1, 10);
      ulong b02 = ROL(a12 ^ d2, 43);
      ulong b12 = ROL(a13 ^ d3, 25);
      ulong b22 = ROL(a14 ^ d4, 39);
      ulong b23 = ROL(a15 ^ d0, 41);
      ulong b08 = ROL(a16 ^ d1, 45);
      ulong b18 = ROL(a17 ^ d2, 15);
      ulong b03 = ROL(a18 ^ d3, 21);
      ulong b13 = ROL(a19 ^ d4, 8);
      ulong b14 = ROL(a20 ^ d0, 18);
      ulong b24 = ROL(a21 ^ d1, 2);
      ulong b09 = ROL(a22 ^ d2, 61);
      ulong b19 = ROL(a23 ^ d3, 56);
      ulong b04 = ROL(a24 ^ d4, 14);
      // chi + iota
      a00 = b00 ^ (~b01 & b02) ^ RC[r];
      a01 = b01 ^ (~b02 & b03);
      a02 = b02 ^ (~b03 & b04);
      a03 = b03 ^ (~b04 & b00);
      a04 = b04 ^ (~b00 & b01);
      a05 = b05 ^ (~b06 & b07);
      a06 = b06 ^ (~b07 & b08);
      a07 = b07 ^ (~b08 & b09);
      a08 = b08 ^ (~b09 & b05);
      a09 = b09 ^ (~b05 & b06);
      a10 = b10 ^ (~b11 & b12);
      a11 = b11 ^ (~b12 & b13);
      a12 = b12 ^ (~b13 & b14);
      a13 = b13 ^ (~b14 & b10);
      a14 = b14 ^ (~b10 & b11);
      a15 = b15 ^ (~b16 & b17);
      a16 = b16 ^ (~b17 & b18);
      a17 = b17 ^ (~b18 & b19);
      a18 = b18 ^ (~b19 & b15);
      a19 = b19 ^ (~b15 & b16);
      a20 = b20 ^ (~b21 & b22);
      a21 = b21 ^ (~b22 & b23);
      a22 = b22 ^ (~b23 & b24);
      a23 = b23 ^ (~b24 & b20);
      a24 = b24 ^ (~b20 & b21);
    }
    if ((a00 & mask) == 0) {
      uint i = atomic_inc(found_count);
      if (i < 1024) found[i] = ctr;
    }
  }
}
