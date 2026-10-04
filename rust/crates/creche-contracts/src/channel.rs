//! The channel protocol between the host and the playpen: the frames and the
//! messages (contract 03).
//!
//! One long-lived channel joins `attendance` on the host and the playpen
//! inside the sandbox of a family. Each record is one line of JSON.
//!
//! | Module | What it holds |
//! |---|---|
//! | [`frame`] | The framing: one record for each line, and the size limit. |
//! | [`host`] | [`host::HostMessage`]: what the host writes and the playpen reads. |
//! | [`claim`] | [`claim::PlaypenLine`]: what the host reads from a line of the playpen. |
//! | [`vocabulary`] | Each closed set of names of a line. |
//! | [`json`], [`text`], [`number`] | The values of a line from the sandbox. |
//!
//! The host side accepts and refuses what the Python implementation does.
//! The vector files under `vectors/data/channel` hold that behavior.

pub mod claim;
pub mod frame;
pub mod host;
pub mod json;
pub mod number;
pub mod text;
pub mod vocabulary;

#[cfg(test)]
mod tests {
    use super::claim::{PlaypenLine, parse};
    use super::frame::{LineSplitter, Refusal};
    use crate::vectors;

    /// What each surface of contract 03 starts with.
    const PREFIX: &str = "channel.";

    /// Each surface of contract 03 in `vectors/data/index.json`, and the test
    /// that walks each vector of the surface.
    const SURFACES: &[(&str, &str)] = &[
        (
            "channel.parse",
            "claim::tests::the_parser_does_with_each_line_what_the_python_host_does",
        ),
        (
            "channel.frame",
            "frame::tests::the_splitter_frames_each_stream_as_the_python_host_does",
        ),
        (
            "channel.build",
            "host::tests::each_host_line_has_the_bytes_of_the_python_host",
        ),
    ];

    #[test]
    fn a_test_walks_each_surface_of_the_channel() {
        let named: Vec<&str> = SURFACES.iter().map(|(surface, _)| *surface).collect();
        let held: Vec<String> = vectors::index()
            .into_iter()
            .map(|row| row.surface)
            .filter(|surface| surface.starts_with(PREFIX))
            .collect();

        assert_eq!(held, named, "the surfaces of the index and of the table");
        for (surface, test) in SURFACES {
            assert!(!vectors::surface(surface).vectors.is_empty(), "{surface}");
            assert!(!test.is_empty(), "{surface}");
        }
    }

    #[test]
    fn bytes_that_are_no_line_are_refused_and_the_channel_stays_usable() {
        let mut splitter = LineSplitter::new();
        let mut stream = Vec::new();
        stream.extend_from_slice(b"\xff\xfe\n");
        stream.extend_from_slice(b"{\"type\":\"pong\",\"nonce\":\"\xed\xa0\x80\"}\n");
        stream.extend_from_slice(b"{\"type\":\"pong\",\"nonce\":\"a\x00b\"}\n");
        stream.extend_from_slice(b"\n");
        stream.extend_from_slice(b"{\"type\":\"pong\",\"nonce\":\"ok\"}\r\n");
        let read: Vec<Result<PlaypenLine, Refusal>> = splitter
            .feed(&stream)
            .iter()
            .map(|line| line.text().and_then(parse))
            .collect();

        assert_eq!(read.len(), 5);
        assert_eq!(read[0], Err(Refusal::BadUtf8));
        assert_eq!(read[1], Err(Refusal::BadUtf8));
        assert_eq!(read[2], Err(Refusal::NotJson));
        assert_eq!(read[3], Err(Refusal::NotJson));
        assert!(matches!(&read[4], Ok(PlaypenLine::Pong(pong)) if pong.nonce() == "ok"));
    }
}
