//! The plane proxies (plan PR V6, decisions CH6-2, CH14, CH62 rule 14,
//! openshell ideas plan idea 1) on real Unix and TCP sockets in the test
//! process. A test plays the guest side, as Cloud Hypervisor connects to
//! `vsock.sock_<port>`, and a TCP listener on 127.0.0.1 plays each plane
//! (decision CH62 rule 28). A scratch family root holds `creds.json`.

use std::io::{self, BufRead, BufReader, ErrorKind, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::thread::spawn;
use std::time::{Duration, Instant};

use creche_vmm::planes::{GATHER_CAP, Planes, SPLICE_CAP, Session, Upstreams, bind, fd_need};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
use creche_vmm_store::placeholder::Placeholders;
use creche_wire::{HostPort, SessionId};
use serde_json::json;
use tokio::runtime::{Builder, Runtime};

/// The bound of each wait of a test.
const WAIT: Duration = Duration::from_secs(5);

/// The family of the scratch root.
const FAMILY: &str = "proof";

/// The real values of `creds.json`.
const LITELLM_KEY: &str = "sk-real-family-key-01";
const PEP_TOKEN: &str = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd";

/// The placeholders of the session.
const LITELLM_PLACEHOLDER: &str = "sk-PlaceHolder_LiteLLM-12";
const PEP_PLACEHOLDER: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

/// The chunks of the burst (S0 of the openshell ideas plan), and the runs
/// of its timing.
const BURST_CHUNKS: usize = 301;
const BURST_RUNS: usize = 100;

/// The run dir of one VM and the family root in `/tmp`, and the runtime
/// of the proxies. Its drop removes the dir, also after a failed assert.
struct Scratch {
    dir: PathBuf,
    run: VmRunDir,
    families: FamilyRoot,
    runtime: Runtime,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
impl Scratch {
    /// `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    /// it, and the temp dir of macOS is too long.
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-planes-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let root = RunRoot::new(dir.join("run")).unwrap();
        let run = root.link_vm(&session_id(), Slot::new(0).unwrap()).unwrap();
        std::fs::create_dir_all(dir.join("families").join(FAMILY).join("creds")).unwrap();
        let families = FamilyRoot::new(dir.join("families")).unwrap();
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let scratch = Self {
            dir,
            run,
            families,
            runtime,
        };
        scratch.write_creds(LITELLM_KEY, PEP_TOKEN);
        scratch
    }

    /// Writes `creds.json` with `key` and `token`, as caregiver does: a
    /// temp file and a rename.
    fn write_creds(&self, key: &str, token: &str) {
        let creds = self.dir.join("families").join(FAMILY).join("creds");
        let text = json!({"epoch": 3, "litellm_key": key, "pep_token": token});
        std::fs::write(creds.join("creds.tmp"), text.to_string()).unwrap();
        std::fs::rename(creds.join("creds.tmp"), creds.join("creds.json")).unwrap();
    }

    /// Binds and serves the proxies of the VM to `upstreams`.
    fn serve(&self, upstreams: Upstreams) -> Planes {
        let bound = bind(&self.run, upstreams).unwrap();
        let session = Session {
            families: self.families.clone(),
            family: FAMILY.parse().unwrap(),
            id: session_id(),
            placeholders: placeholders(),
        };
        let _runtime = self.runtime.enter();
        bound.serve(&session).unwrap()
    }

    /// A guest dial of `port`, as CH makes it.
    fn dial(&self, port: HostPort) -> UnixStream {
        let stream = UnixStream::connect(self.run.host_listener(port)).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream
    }
}

#[cfg(test)]
fn session_id() -> SessionId {
    "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap()
}

#[cfg(test)]
fn placeholders() -> Placeholders {
    let text = json!({"litellm_key": LITELLM_PLACEHOLDER, "pep_token": PEP_PLACEHOLDER});
    serde_json::from_value(text).unwrap()
}

/// The `Authorization` value of the placeholder of `port`.
#[cfg(test)]
fn bearer(port: HostPort) -> String {
    match port {
        HostPort::Litellm => format!("Bearer {LITELLM_PLACEHOLDER}"),
        _ => format!("Bearer {PEP_PLACEHOLDER}"),
    }
}

/// One HTTP/1.1 message: the start line, the headers with lowercase
/// names, and the body of `Content-Length` bytes or of chunks.
#[derive(Debug, Clone)]
struct Message {
    start: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Message {
    #[cfg(test)]
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    #[cfg(test)]
    fn status(&self) -> u16 {
        self.start.split(' ').nth(1).unwrap().parse().unwrap()
    }
}

/// Reads one message. `None` for a link that ends before a start line.
#[cfg(test)]
fn read_message(reader: &mut impl BufRead) -> Option<Message> {
    let mut start = String::new();
    if reader.read_line(&mut start).ok()? == 0 {
        return None;
    }
    let mut headers = Vec::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).ok()?;
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        let (name, value) = line.split_once(':')?;
        headers.push((name.trim().to_ascii_lowercase(), value.trim().to_owned()));
    }
    let mut message = Message {
        start: start.trim_end().to_owned(),
        headers,
        body: Vec::new(),
    };
    if let Some(length) = message.header("content-length") {
        message.body.resize(length.parse().ok()?, 0);
        reader.read_exact(&mut message.body).ok()?;
    } else if message.header("transfer-encoding") == Some("chunked") {
        loop {
            let mut size = String::new();
            reader.read_line(&mut size).ok()?;
            let size = usize::from_str_radix(size.trim(), 16).ok()?;
            let mut chunk = vec![0; size + 2];
            reader.read_exact(&mut chunk).ok()?;
            if size == 0 {
                break;
            }
            message.body.extend_from_slice(&chunk[..size]);
        }
    }
    Some(message)
}

/// The bytes of a request with the header `authorization`, if any, and
/// `body`.
#[cfg(test)]
fn request(path: &str, authorization: &[&str], body: &[u8]) -> Vec<u8> {
    let mut head = format!("POST {path} HTTP/1.1\r\nHost: 127.0.0.1:4000\r\n");
    for value in authorization {
        head.push_str(&format!("Authorization: {value}\r\n"));
    }
    head.push_str(&format!("Content-Length: {}\r\n\r\n", body.len()));
    let mut bytes = head.into_bytes();
    bytes.extend_from_slice(body);
    bytes
}

/// Sends one request on `link` and reads its answer.
#[cfg(test)]
fn exchange(link: &UnixStream, authorization: &[&str], body: &[u8]) -> Message {
    (&*link)
        .write_all(&request("/v1/chat/completions", authorization, body))
        .unwrap();
    read_message(&mut BufReader::new(link)).unwrap()
}

/// How a plane answers one request: the raw bytes, as pieces that each go
/// out in one write.
type Answer = fn(&Message) -> Vec<Vec<u8>>;

/// What one plane got: its connection count, and each request.
struct Plane {
    addr: SocketAddr,
    accepted: Receiver<()>,
    seen: Receiver<Message>,
}

/// A plane on 127.0.0.1. Each connection serves requests with keep-alive
/// until the peer closes. `answer` gives the raw bytes of each answer, as
/// pieces that each go out in one write.
#[cfg(test)]
fn plane(answer: Answer) -> Plane {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (accepted_tx, accepted) = channel();
    let (seen_tx, seen) = channel();
    spawn(move || {
        for stream in listener.incoming() {
            let stream = stream.unwrap();
            stream.set_nodelay(true).unwrap();
            let _ = accepted_tx.send(());
            let seen: Sender<Message> = seen_tx.clone();
            spawn(move || {
                let mut reader = BufReader::new(&stream);
                while let Some(request) = read_message(&mut reader) {
                    for piece in answer(&request) {
                        if (&stream).write_all(&piece).is_err() {
                            return;
                        }
                    }
                    let _ = seen.send(request);
                }
            });
        }
    });
    Plane {
        addr,
        accepted,
        seen,
    }
}

/// Answers 200 with the request body back.
#[cfg(test)]
fn echo(request: &Message) -> Vec<Vec<u8>> {
    let mut answer = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
        request.body.len()
    )
    .into_bytes();
    answer.extend_from_slice(&request.body);
    vec![answer]
}

/// An address where nothing listens: a port that the test bound and
/// closed.
#[cfg(test)]
fn closed_port() -> SocketAddr {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

/// Each row forwards to its own plane: 1101 to LiteLLM with the real key,
/// 1102 to the chaperone with the real token. `Host` and the body pass
/// unchanged (decisions CH14, CH62 rule 20, fact 46).
#[test]
fn each_row_swaps_its_placeholder_for_the_real_value() {
    let scratch = Scratch::new("swap");
    let (litellm, chaperone) = (plane(echo), plane(echo));
    let _planes = scratch.serve(Upstreams {
        litellm: litellm.addr,
        chaperone: chaperone.addr,
    });
    for (port, plane, real) in [
        (HostPort::Litellm, &litellm, LITELLM_KEY),
        (HostPort::Chaperone, &chaperone, PEP_TOKEN),
    ] {
        let link = scratch.dial(port);
        let answer = exchange(&link, &[&bearer(port)], b"{\"model\":\"m\"}");
        assert_eq!(answer.status(), 200, "port {}", port.number());
        assert_eq!(answer.body, b"{\"model\":\"m\"}");
        let seen = plane.seen.recv_timeout(WAIT).unwrap();
        let expected = format!("Bearer {real}");
        assert_eq!(seen.header("authorization"), Some(expected.as_str()));
        assert_eq!(seen.header("host"), Some("127.0.0.1:4000"));
        assert_eq!(seen.start, "POST /v1/chat/completions HTTP/1.1");
    }
}

/// No header, two headers, a real value, the placeholder of the other
/// port and a placeholder with another scheme each get 403, and the plane
/// sees no request.
#[test]
fn a_wrong_authorization_gets_403_and_reaches_no_plane() {
    let scratch = Scratch::new("refuse");
    let litellm = plane(echo);
    let _planes = scratch.serve(Upstreams {
        litellm: litellm.addr,
        chaperone: closed_port(),
    });
    let good = bearer(HostPort::Litellm);
    let real = format!("Bearer {LITELLM_KEY}");
    let other = bearer(HostPort::Chaperone);
    let basic = format!("Basic {LITELLM_PLACEHOLDER}");
    let cases: [&[&str]; 5] = [&[], &[&good, &good], &[&real], &[&other], &[&basic]];
    let link = scratch.dial(HostPort::Litellm);
    for authorization in cases {
        let answer = exchange(&link, authorization, b"x");
        assert_eq!(answer.status(), 403, "{authorization:?}");
        assert!(answer.body.is_empty());
    }
    assert!(
        litellm
            .seen
            .recv_timeout(Duration::from_millis(200))
            .is_err()
    );
    assert!(litellm.accepted.try_recv().is_err());
    // The same link still serves a good request after the refusals.
    assert_eq!(exchange(&link, &[&good], b"x").status(), 200);
}

/// A `creds.json` that does not read gets 503, and the plane sees no
/// request.
#[test]
fn no_creds_gets_503() {
    let scratch = Scratch::new("no-creds");
    let litellm = plane(echo);
    let _planes = scratch.serve(Upstreams {
        litellm: litellm.addr,
        chaperone: closed_port(),
    });
    let creds = scratch.dir.join("families").join(FAMILY).join("creds");
    std::fs::remove_file(creds.join("creds.json")).unwrap();
    let link = scratch.dial(HostPort::Litellm);
    assert_eq!(
        exchange(&link, &[&bearer(HostPort::Litellm)], b"x").status(),
        503
    );
    assert!(
        litellm
            .seen
            .recv_timeout(Duration::from_millis(200))
            .is_err()
    );
}

/// The proxy reads `creds.json` per request, so a rotation reaches the
/// next request on the same link (openshell ideas plan, section 3.2).
#[test]
fn a_rotation_reaches_the_next_request() {
    let scratch = Scratch::new("rotate");
    let chaperone = plane(echo);
    let _planes = scratch.serve(Upstreams {
        litellm: closed_port(),
        chaperone: chaperone.addr,
    });
    let link = scratch.dial(HostPort::Chaperone);
    let good = bearer(HostPort::Chaperone);
    assert_eq!(exchange(&link, &[&good], b"1").status(), 200);
    let first = chaperone.seen.recv_timeout(WAIT).unwrap();
    assert_eq!(
        first.header("authorization"),
        Some(format!("Bearer {PEP_TOKEN}").as_str())
    );
    let rotated = "ef".repeat(32);
    scratch.write_creds(LITELLM_KEY, &rotated);
    assert_eq!(exchange(&link, &[&good], b"2").status(), 200);
    let second = chaperone.seen.recv_timeout(WAIT).unwrap();
    assert_eq!(
        second.header("authorization"),
        Some(format!("Bearer {rotated}").as_str())
    );
}

/// Many requests on one guest link use one upstream connection, and a
/// body of 1 MiB passes both ways unchanged.
#[test]
fn one_guest_link_keeps_one_upstream_and_streams_large_bodies() {
    let scratch = Scratch::new("keep");
    let litellm = plane(echo);
    let _planes = scratch.serve(Upstreams {
        litellm: litellm.addr,
        chaperone: closed_port(),
    });
    let body: Vec<u8> = (0..1_u32 << 20)
        .map(|index| {
            let [low, high, ..] = index.to_le_bytes();
            low ^ high
        })
        .collect();
    let link = scratch.dial(HostPort::Litellm);
    for _ in 0..5 {
        let answer = exchange(&link, &[&bearer(HostPort::Litellm)], &body);
        assert_eq!(answer.status(), 200);
        assert!(answer.body == body);
    }
    litellm.accepted.recv_timeout(WAIT).unwrap();
    assert!(litellm.accepted.try_recv().is_err(), "a second connect");
}

/// A plane that refuses the connect gets 502 inside `WAIT`, and nothing
/// retries (decision CH62 rule 14).
#[test]
fn a_refused_upstream_gets_502() {
    let scratch = Scratch::new("refused");
    let _planes = scratch.serve(Upstreams {
        litellm: closed_port(),
        chaperone: closed_port(),
    });
    for port in [HostPort::Litellm, HostPort::Chaperone] {
        let started = Instant::now();
        let link = scratch.dial(port);
        assert_eq!(exchange(&link, &[&bearer(port)], b"x").status(), 502);
        assert!(started.elapsed() < WAIT);
    }
}

/// Reads the request and never answers, so each connection stays open
/// with a request in flight.
#[cfg(test)]
fn silent(_request: &Message) -> Vec<Vec<u8>> {
    std::thread::sleep(Duration::from_secs(3600));
    Vec::new()
}

/// One row holds `SPLICE_CAP` open connections. The next dial closes at
/// once. After one connection ends, a dial gets through again (decision
/// CH62 rule 14).
#[test]
fn refuses_past_the_connection_cap() {
    let scratch = Scratch::new("cap");
    let litellm = plane(silent);
    let _planes = scratch.serve(Upstreams {
        litellm: litellm.addr,
        chaperone: closed_port(),
    });
    let send = |link: &UnixStream| {
        let bytes = request("/", &[&bearer(HostPort::Litellm)], b"x");
        (&*link).write_all(&bytes).unwrap();
    };
    let mut held: Vec<UnixStream> = (0..SPLICE_CAP)
        .map(|_| {
            let link = scratch.dial(HostPort::Litellm);
            send(&link);
            litellm.accepted.recv_timeout(WAIT).unwrap();
            link
        })
        .collect();

    let mut refused = scratch.dial(HostPort::Litellm);
    let mut rest = Vec::new();
    assert_eq!(refused.read_to_end(&mut rest).unwrap(), 0);

    held.pop().unwrap().shutdown(Shutdown::Both).unwrap();
    let deadline = Instant::now() + WAIT;
    loop {
        assert!(Instant::now() < deadline, "no dial got through");
        let link = scratch.dial(HostPort::Litellm);
        // A refused link closes before it reads, so the write may fail.
        let bytes = request("/", &[&bearer(HostPort::Litellm)], b"x");
        let _ = (&link).write_all(&bytes);
        if litellm
            .accepted
            .recv_timeout(Duration::from_millis(50))
            .is_ok()
        {
            held.push(link);
            break;
        }
    }
    assert_eq!(held.len(), SPLICE_CAP);
}

/// The drop of the proxies closes each open connection and each listener,
/// as a VM leaves the start table.
#[test]
fn a_drop_closes_the_listeners_and_the_connections() {
    let scratch = Scratch::new("drop");
    let planes = scratch.serve(Upstreams {
        litellm: plane(echo).addr,
        chaperone: plane(echo).addr,
    });
    let mut link = scratch.dial(HostPort::Litellm);
    assert_eq!(
        exchange(&link, &[&bearer(HostPort::Litellm)], b"x").status(),
        200
    );

    drop(planes);
    let mut rest = Vec::new();
    assert_eq!(link.read_to_end(&mut rest).unwrap(), 0);
    let deadline = Instant::now() + WAIT;
    for port in [HostPort::Litellm, HostPort::Chaperone] {
        let path = scratch.run.host_listener(port);
        while UnixStream::connect(&path).is_ok() {
            assert!(
                Instant::now() < deadline,
                "port {} still listens",
                port.number()
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let error = UnixStream::connect(&path).unwrap_err();
        assert_eq!(error.kind(), ErrorKind::ConnectionRefused);
    }
}

/// Answers a stream of `BURST_CHUNKS` chunks, each one write, back to
/// back, as the burst row of S0.
#[cfg(test)]
fn burst_each(_request: &Message) -> Vec<Vec<u8>> {
    let head =
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n";
    let mut answer = vec![head.to_vec()];
    for index in 0..BURST_CHUNKS {
        let data = format!("data: {{\"chunk\":{index},\"text\":\"token\"}}\n\n");
        answer.push(format!("{:x}\r\n{data}\r\n", data.len()).into_bytes());
    }
    answer.push(b"0\r\n\r\n".to_vec());
    answer
}

/// The stream of `burst_each` in one write, so the proxy reads every
/// chunk at once.
#[cfg(test)]
fn burst_whole(request: &Message) -> Vec<Vec<u8>> {
    vec![burst_each(request).concat()]
}

/// The time of `runs` requests on one link `link`, each from the write
/// of the request to the last byte of its body.
#[cfg(test)]
fn time_burst<S>(link: &S, authorization: &str, runs: usize) -> Vec<Duration>
where
    for<'a> &'a S: Read + Write,
{
    let mut reader = BufReader::new(link);
    (0..runs)
        .map(|_| {
            let began = Instant::now();
            let mut writer = link;
            writer
                .write_all(&request("/v1/chat/completions", &[authorization], b"{}"))
                .unwrap();
            let answer = read_message(&mut reader).unwrap();
            let took = began.elapsed();
            assert_eq!(answer.status(), 200);
            assert!(answer.body.starts_with(b"data: {\"chunk\":0,"));
            assert!(
                answer
                    .body
                    .ends_with(b"\"chunk\":300,\"text\":\"token\"}\n\n")
            );
            took
        })
        .collect()
}

/// The `percent` percentile of `times` in µs.
#[cfg(test)]
fn percentile(times: &mut [Duration], percent: usize) -> u128 {
    times.sort();
    let index = (times.len() * percent / 100).min(times.len() - 1);
    times[index].as_micros()
}

/// The timing line of `shape` on `answer`: `BURST_RUNS` requests on one
/// kept-alive link, end of stream, through the proxy and on a direct TCP
/// link to the same plane.
#[cfg(test)]
fn burst_line(shape: &str, answer: Answer) -> String {
    let scratch = Scratch::new(shape);
    let litellm = plane(answer);
    let _planes = scratch.serve(Upstreams {
        litellm: litellm.addr,
        chaperone: closed_port(),
    });
    let direct = TcpStream::connect(litellm.addr).unwrap();
    direct.set_nodelay(true).unwrap();
    direct.set_read_timeout(Some(WAIT)).unwrap();
    let proxied = scratch.dial(HostPort::Litellm);
    let good = bearer(HostPort::Litellm);
    // One warm-up request on each link.
    time_burst(&direct, &good, 1);
    time_burst(&proxied, &good, 1);
    let mut direct_times = time_burst(&direct, &good, BURST_RUNS);
    let mut proxied_times = time_burst(&proxied, &good, BURST_RUNS);
    let (direct_p50, direct_p99) = (
        percentile(&mut direct_times, 50),
        percentile(&mut direct_times, 99),
    );
    let (proxied_p50, proxied_p99) = (
        percentile(&mut proxied_times, 50),
        percentile(&mut proxied_times, 99),
    );
    let added = i128::try_from(proxied_p50).unwrap() - i128::try_from(direct_p50).unwrap();
    format!(
        "plane burst {shape}: {BURST_CHUNKS} chunks, {BURST_RUNS} runs, end of stream: direct p50 {direct_p50} us p99 {direct_p99} us, proxied p50 {proxied_p50} us p99 {proxied_p99} us, added p50 {added} us\n"
    )
}

/// A burst of 301 chunks back to back passes whole and in order through
/// the proxy (S0 note 2), in one write per chunk and in one write. The
/// test times both and writes the lines to the job log. It asserts no
/// time: a CI host has no stable clock budget.
#[test]
fn a_burst_of_301_chunks_passes_whole() {
    const { assert!(GATHER_CAP >= 64 * 1024) };
    let shapes: [(&str, Answer); 2] = [("each-write", burst_each), ("one-write", burst_whole)];
    for (shape, answer) in shapes {
        // A direct write skips the capture of the test harness, so the
        // job log holds the line.
        io::stderr()
            .write_all(burst_line(shape, answer).as_bytes())
            .unwrap();
    }
}

/// Each of the 64 slots with both rows full: per row the listener and per
/// connection two streams and three fds of one `creds.json` read,
/// 64 × 2 × (64 × 5 + 1), plus the headroom of 4096. The vmm raises its
/// soft fd limit to it at start.
#[test]
fn the_fd_need_holds_every_full_proxy() {
    assert_eq!(SPLICE_CAP, 64);
    assert_eq!(fd_need(), 64 * 2 * (64 * 5 + 1) + 4096);
}
