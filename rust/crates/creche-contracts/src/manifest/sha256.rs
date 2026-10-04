//! SHA-256 (FIPS 180-4), for the hash of a resolved manifest.
//!
//! The crate has no dependency that gives a hash. The input here is public
//! text, so the time of the function tells nothing. The tests hold the answers
//! of FIPS 180-4, and the differential test holds the answers of Python
//! `hashlib`.

/// The count of bytes in one block.
const BLOCK_BYTES: usize = 64;

/// The count of bytes in one digest.
pub(super) const DIGEST_BYTES: usize = 32;

/// The count of bytes that hold the bit count at the end of the last block.
const LENGTH_BYTES: usize = 8;

/// The first 32 bits of the fraction of the cube root of each of the first 64
/// primes.
const ROUND: [u32; 64] = [
    0x428a_2f98,
    0x7137_4491,
    0xb5c0_fbcf,
    0xe9b5_dba5,
    0x3956_c25b,
    0x59f1_11f1,
    0x923f_82a4,
    0xab1c_5ed5,
    0xd807_aa98,
    0x1283_5b01,
    0x2431_85be,
    0x550c_7dc3,
    0x72be_5d74,
    0x80de_b1fe,
    0x9bdc_06a7,
    0xc19b_f174,
    0xe49b_69c1,
    0xefbe_4786,
    0x0fc1_9dc6,
    0x240c_a1cc,
    0x2de9_2c6f,
    0x4a74_84aa,
    0x5cb0_a9dc,
    0x76f9_88da,
    0x983e_5152,
    0xa831_c66d,
    0xb003_27c8,
    0xbf59_7fc7,
    0xc6e0_0bf3,
    0xd5a7_9147,
    0x06ca_6351,
    0x1429_2967,
    0x27b7_0a85,
    0x2e1b_2138,
    0x4d2c_6dfc,
    0x5338_0d13,
    0x650a_7354,
    0x766a_0abb,
    0x81c2_c92e,
    0x9272_2c85,
    0xa2bf_e8a1,
    0xa81a_664b,
    0xc24b_8b70,
    0xc76c_51a3,
    0xd192_e819,
    0xd699_0624,
    0xf40e_3585,
    0x106a_a070,
    0x19a4_c116,
    0x1e37_6c08,
    0x2748_774c,
    0x34b0_bcb5,
    0x391c_0cb3,
    0x4ed8_aa4a,
    0x5b9c_ca4f,
    0x682e_6ff3,
    0x748f_82ee,
    0x78a5_636f,
    0x84c8_7814,
    0x8cc7_0208,
    0x90be_fffa,
    0xa450_6ceb,
    0xbef9_a3f7,
    0xc671_78f2,
];

/// The first 32 bits of the fraction of the square root of each of the first 8
/// primes.
const START: [u32; 8] = [
    0x6a09_e667,
    0xbb67_ae85,
    0x3c6e_f372,
    0xa54f_f53a,
    0x510e_527f,
    0x9b05_688c,
    0x1f83_d9ab,
    0x5be0_cd19,
];

/// Mixes one block of 64 bytes into the state.
fn compress(state: &mut [u32; 8], block: &[u8]) {
    let mut schedule = [0_u32; 64];
    for (word, bytes) in schedule.iter_mut().zip(block.chunks_exact(4)) {
        *word = bytes
            .iter()
            .fold(0, |word, byte| (word << 8) | u32::from(*byte));
    }

    for index in 16..schedule.len() {
        let words = (
            schedule.get(index - 16).copied(),
            schedule.get(index - 15).copied(),
            schedule.get(index - 7).copied(),
            schedule.get(index - 2).copied(),
        );
        // Each index is below 64, so each word is there.
        let (Some(first), Some(second), Some(seventh), Some(last)) = words else {
            continue;
        };
        let low = second.rotate_right(7) ^ second.rotate_right(18) ^ (second >> 3);
        let high = last.rotate_right(17) ^ last.rotate_right(19) ^ (last >> 10);
        let next = first
            .wrapping_add(low)
            .wrapping_add(seventh)
            .wrapping_add(high);
        if let Some(word) = schedule.get_mut(index) {
            *word = next;
        }
    }

    let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = *state;
    for (constant, word) in ROUND.iter().zip(schedule) {
        let sum_e = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
        let choice = (e & f) ^ (!e & g);
        let first = h
            .wrapping_add(sum_e)
            .wrapping_add(choice)
            .wrapping_add(*constant)
            .wrapping_add(word);
        let sum_a = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
        let majority = (a & b) ^ (a & c) ^ (b & c);
        let second = sum_a.wrapping_add(majority);
        h = g;
        g = f;
        f = e;
        e = d.wrapping_add(first);
        d = c;
        c = b;
        b = a;
        a = first.wrapping_add(second);
    }

    for (word, mixed) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
        *word = word.wrapping_add(mixed);
    }
}

/// The SHA-256 digest of `message`.
pub(super) fn digest(message: &[u8]) -> [u8; DIGEST_BYTES] {
    let mut state = START;
    let mut blocks = message.chunks_exact(BLOCK_BYTES);
    for block in &mut blocks {
        compress(&mut state, block);
    }

    // A slice has `isize::MAX` bytes at most, so the count of bits fits.
    let bits = u64::try_from(message.len())
        .unwrap_or(u64::MAX)
        .wrapping_mul(8);
    let mut tail = blocks.remainder().to_vec();
    tail.push(0x80);
    while tail.len() % BLOCK_BYTES != BLOCK_BYTES - LENGTH_BYTES {
        tail.push(0);
    }

    tail.extend_from_slice(&bits.to_be_bytes());
    for block in tail.chunks_exact(BLOCK_BYTES) {
        compress(&mut state, block);
    }

    let mut out = [0_u8; DIGEST_BYTES];
    for (bytes, word) in out.chunks_exact_mut(4).zip(state) {
        bytes.copy_from_slice(&word.to_be_bytes());
    }

    out
}

/// The SHA-256 digest of `message` as 64 lower-case hex bytes.
pub(super) fn hex_digest(message: &[u8]) -> String {
    digest(message)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_digest_of_each_fips_message_is_the_fips_answer() {
        let table = [
            (
                "",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ),
            (
                "abc",
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            ),
            (
                "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
                "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
            ),
            (
                "abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu",
                "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1",
            ),
        ];
        for (message, answer) in table {
            assert_eq!(hex_digest(message.as_bytes()), answer, "{message:?}");
        }
    }

    #[test]
    fn a_million_bytes_give_the_fips_answer() {
        let message = vec![b'a'; 1_000_000];

        assert_eq!(
            hex_digest(&message),
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
        );
    }

    #[test]
    fn each_length_near_a_block_edge_has_its_own_digest() {
        // The pad changes form at 55, 56, 63 and 64 bytes.
        let table = [
            (
                55,
                "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318",
            ),
            (
                56,
                "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a",
            ),
            (
                63,
                "7d3e74a05d7db15bce4ad9ec0658ea98e3f06eeecf16b4c6fff2da457ddc2f34",
            ),
            (
                64,
                "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb",
            ),
            (
                65,
                "635361c48bb9eab14198e76ea8ab7f1a41685d6ad62aa9146d301d4f17eb0ae0",
            ),
        ];
        for (length, answer) in table {
            assert_eq!(hex_digest(&vec![b'a'; length]), answer, "{length} bytes");
        }
    }
}
