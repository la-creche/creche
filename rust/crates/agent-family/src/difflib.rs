//! The nearest known name for an unknown field: a port of
//! `difflib.get_close_matches` of Python, as the Python validator calls it.
//!
//! The message for an unknown field names the nearest known field (contract
//! 01 §7 rule 1). The Python validator finds it with the standard library,
//! so this module computes the same ratio in the same way.

use std::collections::{HashMap, HashSet};

/// A name is near when its ratio is this number or more.
const CUTOFF: f64 = 0.6;

/// Python drops a frequent character of a long text from the search. A text
/// is long from this count of characters on.
const AUTOJUNK_MIN: usize = 200;

/// The positions of each character of `b`, without the characters that
/// Python takes as frequent (`SequenceMatcher.__chain_b`).
fn positions(b: &[char]) -> (HashMap<char, Vec<usize>>, HashSet<char>) {
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (index, c) in b.iter().enumerate() {
        b2j.entry(*c).or_default().push(index);
    }

    let mut popular = HashSet::new();
    if b.len() >= AUTOJUNK_MIN {
        let limit = b.len() / 100 + 1;
        popular = b2j
            .iter()
            .filter(|(_, indices)| indices.len() > limit)
            .map(|(c, _)| *c)
            .collect();
        b2j.retain(|c, _| !popular.contains(c));
    }

    (b2j, popular)
}

/// One part of `a` and one part of `b`, each with its first index and the
/// index after its last.
#[derive(Debug, Clone, Copy)]
struct Window {
    alo: usize,
    ahi: usize,
    blo: usize,
    bhi: usize,
}

/// `SequenceMatcher.find_longest_match`: the first index in `a`, the first
/// index in `b` and the size of the longest block that both parts hold.
fn longest_match(
    a: &[char],
    b: &[char],
    b2j: &HashMap<char, Vec<usize>>,
    window: Window,
) -> (usize, usize, usize) {
    let Window { alo, ahi, blo, bhi } = window;
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0);
    let mut j2len: HashMap<usize, usize> = HashMap::new();
    for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
        let mut newj2len: HashMap<usize, usize> = HashMap::new();
        for j in b2j.get(c).into_iter().flatten().copied() {
            if j < blo {
                continue;
            }

            if j >= bhi {
                break;
            }

            let before = j.checked_sub(1).and_then(|j| j2len.get(&j)).copied();
            let k = before.unwrap_or(0) + 1;
            newj2len.insert(j, k);
            if k > bestsize {
                (besti, bestj, bestsize) = (i + 1 - k, j + 1 - k, k);
            }
        }

        j2len = newj2len;
    }

    // Python grows the block over each equal character at both ends. It has
    // no junk here, so one pass at each end is the whole of its four loops.
    while besti > alo && bestj > blo && a.get(besti - 1) == b.get(bestj - 1) {
        (besti, bestj, bestsize) = (besti - 1, bestj - 1, bestsize + 1);
    }

    while besti + bestsize < ahi
        && bestj + bestsize < bhi
        && a.get(besti + bestsize) == b.get(bestj + bestsize)
    {
        bestsize += 1;
    }

    (besti, bestj, bestsize)
}

/// The count of characters in the blocks that `a` and `b` share
/// (`SequenceMatcher.get_matching_blocks`).
fn matched(a: &[char], b: &[char]) -> usize {
    let (b2j, _) = positions(b);
    let mut total = 0;
    let mut queue = vec![Window {
        alo: 0,
        ahi: a.len(),
        blo: 0,
        bhi: b.len(),
    }];
    while let Some(window) = queue.pop() {
        let (i, j, k) = longest_match(a, b, &b2j, window);
        if k == 0 {
            continue;
        }

        total += k;
        if window.alo < i && window.blo < j {
            queue.push(Window {
                ahi: i,
                bhi: j,
                ..window
            });
        }

        if i + k < window.ahi && j + k < window.bhi {
            queue.push(Window {
                alo: i + k,
                blo: j + k,
                ..window
            });
        }
    }

    total
}

/// A count as a float. A count of characters is far below the largest
/// integer that a float holds exactly.
fn as_float(count: usize) -> f64 {
    u32::try_from(count).map_or(f64::MAX, f64::from)
}

/// `SequenceMatcher(None, a, b).ratio()`.
fn ratio(a: &[char], b: &[char]) -> f64 {
    let length = a.len() + b.len();
    if length == 0 {
        return 1.0;
    }

    2.0 * as_float(matched(a, b)) / as_float(length)
}

/// `difflib.get_close_matches(unknown, known, n=1, cutoff=0.6)`: the known
/// name with the largest ratio. Between two names with one ratio, Python
/// takes the later name in the order of text.
pub(crate) fn closest_name<'a>(unknown: &str, known: &[&'a str]) -> Option<&'a str> {
    let word: Vec<char> = unknown.chars().collect();
    let mut best: Option<(f64, &str)> = None;
    for name in known {
        let letters: Vec<char> = name.chars().collect();
        let score = ratio(&letters, &word);
        if score < CUTOFF {
            continue;
        }

        if best.is_none_or(|top| (score, *name) > top) {
            best = Some((score, name));
        }
    }

    best.map(|(_, name)| name)
}

#[cfg(test)]
mod tests {
    use super::{closest_name, ratio};

    fn chars(text: &str) -> Vec<char> {
        text.chars().collect()
    }

    /// What Python 3.13 answers for `SequenceMatcher(None, a, b).ratio()`.
    #[test]
    fn a_ratio_is_the_python_ratio() {
        let cases = [
            ("description", "descripton", 0.952_380_952_380_952_4),
            ("kind", "KIND", 0.0),
            ("shell", "shel", 0.888_888_888_888_888_8),
            ("skills", "shel", 0.4),
            ("abcd", "bcde", 0.75),
            ("", "", 1.0),
            ("a", "", 0.0),
            ("max_running_turns", "max_runing_turn", 0.937_5),
        ];
        for (a, b, wanted) in cases {
            let got = ratio(&chars(a), &chars(b));
            assert!((got - wanted).abs() < 1e-15, "{a} {b}: {got}");
        }
    }

    #[test]
    fn the_nearest_name_is_the_python_answer() {
        let known = ["name", "kind", "description", "shell", "skills", "sandbox"];
        assert_eq!(closest_name("descripton", &known), Some("description"));
        assert_eq!(closest_name("shel", &known), Some("shell"));
        assert_eq!(closest_name("zzzzqqq", &known), None);
        assert_eq!(closest_name("", &known), None);
        assert_eq!(closest_name("KIND", &known), None);
    }
}
