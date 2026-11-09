//! The kvm_ test of the real facade (decisions CH62 rule 12, CH80): the
//! phase 3 proof of `tests/kvm_guest.rs`, with the base disk that
//! `bin/vm-smoke-setup.sh` builds from the tree of the run in place of the
//! stand-in. So a facade change is tested on its own build. The disk holds
//! guest init's mount point of the session disk (CH62 rule 5) and the
//! facade with its CH mode: the fixed guest paths, the start state from
//! one pull on 1100, no lock, no beat deadline and no ACP on stdio.
//!
//! `examples/phase3_vm.rs` plays the vmm as a transient user service
//! (decisions CH60 rule 1, CH62 rule 29). The stub planes are Rust
//! listeners in the test (decision CH62 rule 28):
//!
//! - LiteLLM answers `GET /v1/model/info` and `POST /chat/completions`
//!   with an OpenAI SSE stream, as `agent/test/fake-model.ts` does.
//! - The chaperone answers MCP streamable HTTP on `/mcp`: `initialize`,
//!   `notifications/initialized`, `tools/list` with one tool and
//!   `tools/call`, as `agent/test/fake-ch.ts` does.
//!
//! Flue keeps one MCP connection for the life of a session (`mcp-*.mjs`
//! of Flue 2.3.0), so a second prompt sends no `tools/list`. The second
//! prompt therefore asks for the one tool, and its `tools/call` hits the
//! chaperone after the restore.
//!
//! `#[ignore]`: only the job `kvm` runs it, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`.
#![cfg(target_os = "linux")]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, BufRead as _, BufReader, Read as _, Write as _};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use creche_vmm_store::claim::CONSUMED;
use creche_vmm_store::layout::SessionDir;
use creche_wire::guest::{CHAPERONE_PORT, FACADE_STOP_WAIT, LITELLM_PORT};
use creche_wire::{FacadeState, GuestPort};
use serde_json::{Value, json};

use common::{
    BOOT, LITELLM_KEY, PEP_TOKEN, RootDisk, SESSION, Shape, UP, Vmm, boot_with, files, lines,
};

/// The base disk of the setup script: the `agent-sandbox` target of
/// `playpen/Dockerfile` from the tree of the run, as `playpen-disk` writes
/// it (decisions CH7-2, CH75 rule 2, CH80).
const BASE_DISK: &str = "base.ext4.zst";

/// The digest line of `BASE_DISK`, as `playpen-disk` prints it: the
/// SHA-256 of the decompressed disk, two spaces and `base.ext4` (decision
/// CH41 rule 1).
const BASE_DIGEST: &str = "base.sha256";

/// The VM shape of the proof. Node and Flue need more than the 256 MiB of
/// the stand-in.
const SHAPE: Shape = Shape { memory_mib: 1024 };

/// How long the facade has to open its local link after the cold boot:
/// the Node start on 1 vCPU, the start pull, the limits read and the Flue
/// start. No run measured it before this test (plan notes, risk 2).
const FACADE_UP: Duration = Duration::from_secs(120);

/// How long one ACP answer may take.
const ANSWER: Duration = Duration::from_secs(60);

/// The host programs that read the session disk after the stop.
const E2FSCK: &str = "/usr/sbin/e2fsck";
const DUMPE2FS: &str = "/usr/sbin/dumpe2fs";
const DEBUGFS: &str = "/usr/sbin/debugfs";

/// The bare alias of the family: `litellm/test` in `runtime.json`.
const ALIAS: &str = "test";

/// The prompts of the proof. The second one asks for the stub tool.
const FIRST_PROMPT: &str = "Say hello.";
const SECOND_PROMPT: &str = "Call the stub tool, then say what it gave.";

/// The marker that makes the stub model call the tool.
const TOOL_ASK: &str = "stub tool";

/// The one tool of the stub chaperone, and its name for the model:
/// `mcp__<connection>__<tool>`, with the connection `ch`.
const TOOL: &str = "proof__echo";
const MODEL_TOOL: &str = "mcp__ch__proof__echo";

/// The texts of the stub planes.
const HELLO: &str = "STUB-HELLO from the stub model.";
const ECHO: &str = "STUB-ECHO";
const AFTER_TOOL: &str = "STUB-AFTER-TOOL: the echo came back.";

/// The stop line of guest init, as the facade writes it to the console.
const STOP_LINE: &str = "creche-facade: the stop line of guest init";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// One request that a stub plane got: the method, the path, the `Host`
/// and `Authorization` headers and the body.
#[derive(Debug, Clone)]
struct Seen {
    method: String,
    path: String,
    host: String,
    authorization: String,
    body: Value,
}

/// One answer of a stub plane.
#[derive(Debug)]
struct Answer {
    status: &'static str,
    content_type: &'static str,
    body: Vec<u8>,
}

impl Answer {
    #[cfg(test)]
    fn json(value: &Value) -> Self {
        Self {
            status: "200 OK",
            content_type: "application/json",
            body: serde_json::to_vec(value).unwrap(),
        }
    }

    #[cfg(test)]
    fn empty(status: &'static str) -> Self {
        Self {
            status,
            content_type: "text/plain",
            body: Vec::new(),
        }
    }
}

/// Reads one HTTP/1.1 request: the request line, the headers and a body
/// of `Content-Length` bytes or of chunks. `None` for a link that ends
/// first.
#[cfg(test)]
fn read_request(reader: &mut BufReader<&TcpStream>) -> Option<Seen> {
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    let mut words = line.split_whitespace();
    let (method, path) = (words.next()?.to_owned(), words.next()?.to_owned());
    let mut headers = BTreeMap::new();
    loop {
        let mut header = String::new();
        reader.read_line(&mut header).ok()?;
        let header = header.trim_end();
        if header.is_empty() {
            break;
        }
        let (name, value) = header.split_once(':')?;
        headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_owned());
    }
    let mut body = Vec::new();
    if let Some(length) = headers.get("content-length") {
        body.resize(length.parse().ok()?, 0);
        reader.read_exact(&mut body).ok()?;
    } else if headers
        .get("transfer-encoding")
        .is_some_and(|value| value == "chunked")
    {
        loop {
            let mut size = String::new();
            reader.read_line(&mut size).ok()?;
            let size = usize::from_str_radix(size.trim(), 16).ok()?;
            let mut chunk = vec![0; size + 2];
            reader.read_exact(&mut chunk).ok()?;
            if size == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..size]);
        }
    }
    let header = |name: &str| headers.get(name).cloned().unwrap_or_default();
    Some(Seen {
        method,
        path,
        host: header("host"),
        authorization: header("authorization"),
        body: serde_json::from_slice(&body).unwrap_or(Value::Null),
    })
}

/// One stub plane on 127.0.0.1 with a port from the kernel (decision
/// CH62 rule 28). Each link gets a thread, one request and the answer of
/// `answer`, then a close, so no link outlives a snapshot. Each request
/// goes on the channel after its answer.
#[cfg(test)]
fn stub(answer: fn(&Seen) -> Answer) -> (SocketAddr, Receiver<Seen>) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let (seen, received) = mpsc::channel();
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let seen: Sender<Seen> = seen.clone();
            thread::spawn(move || {
                let mut reader = BufReader::new(&stream);
                let Some(request) = read_request(&mut reader) else {
                    return;
                };
                let reply = answer(&request);
                let head = format!(
                    "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    reply.status,
                    reply.content_type,
                    reply.body.len()
                );
                let _ = (&stream).write_all(head.as_bytes());
                let _ = (&stream).write_all(&reply.body);
                let _ = seen.send(request);
            });
        }
    });
    (address, received)
}

/// The SSE body of one streamed chat completion: one delta, the finish
/// reason, the usage, then `[DONE]`, as `agent/test/fake-model.ts`
/// streams it.
#[cfg(test)]
fn completion(delta: Value, finish: &str) -> Answer {
    let base =
        json!({"id": "stub", "object": "chat.completion.chunk", "created": 0, "model": ALIAS});
    let with = |extra: Value| {
        let mut chunk = base.clone();
        chunk
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        chunk
    };
    let chunks = [
        with(json!({"choices": [{"index": 0, "delta": delta, "finish_reason": null}]})),
        with(json!({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})),
        with(
            json!({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}),
        ),
    ];
    let mut body = String::new();
    for chunk in chunks {
        body.push_str(&format!("data: {chunk}\n\n"));
    }
    body.push_str("data: [DONE]\n\n");
    Answer {
        status: "200 OK",
        content_type: "text/event-stream",
        body: body.into_bytes(),
    }
}

/// The stub of LiteLLM. The model list names the alias with its limits.
/// A completion whose last message is a tool result gets `AFTER_TOOL`, a
/// completion whose last message asks for the stub tool gets a call of
/// it, and any other gets `HELLO`.
#[cfg(test)]
fn litellm(request: &Seen) -> Answer {
    if request.method == "GET" && request.path == "/v1/model/info" {
        let info = json!({"max_input_tokens": 32_000, "max_output_tokens": 1024});
        return Answer::json(&json!({"data": [{"model_name": ALIAS, "model_info": info}]}));
    }
    if request.method != "POST" || !request.path.ends_with("/chat/completions") {
        return Answer::empty("404 Not Found");
    }
    let last = request.body["messages"]
        .as_array()
        .and_then(|messages| messages.last())
        .cloned()
        .unwrap_or(Value::Null);
    if last["role"] == "tool" {
        return completion(json!({"role": "assistant", "content": AFTER_TOOL}), "stop");
    }
    if last.to_string().contains(TOOL_ASK) {
        let call = json!({
            "index": 0,
            "id": "call_stub_1",
            "type": "function",
            "function": {"name": MODEL_TOOL, "arguments": r#"{"text":"hi"}"#},
        });
        return completion(
            json!({"role": "assistant", "content": null, "tool_calls": [call]}),
            "tool_calls",
        );
    }
    completion(json!({"role": "assistant", "content": HELLO}), "stop")
}

/// The stub of the chaperone's front door: MCP streamable HTTP, JSON
/// answers only. A notification gets 202, a GET 405 (no server stream)
/// and a DELETE 200.
#[cfg(test)]
fn chaperone(request: &Seen) -> Answer {
    if request.path != "/mcp" {
        return Answer::empty("404 Not Found");
    }
    match request.method.as_str() {
        "POST" => {}
        "DELETE" => return Answer::empty("200 OK"),
        _ => return Answer::empty("405 Method Not Allowed"),
    }
    let rpc = &request.body;
    let Some(id) = rpc.get("id") else {
        return Answer::empty("202 Accepted");
    };
    let schema =
        json!({"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]});
    let reply = match rpc["method"].as_str() {
        Some("initialize") => json!({"result": {
            "protocolVersion": rpc["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub-ch", "version": "0.0.0"},
        }}),
        Some("tools/list") => json!({"result": {"tools": [
            {"name": TOOL, "description": "Echo the text.", "inputSchema": schema},
        ]}}),
        Some("tools/call") => json!({"result": {"content": [{"type": "text", "text": ECHO}]}}),
        _ => json!({"error": {"code": -32601, "message": "method not found"}}),
    };
    let mut answer = json!({"jsonrpc": "2.0", "id": id});
    answer
        .as_object_mut()
        .unwrap()
        .extend(reply.as_object().unwrap().clone());
    Answer::json(&answer)
}

/// Waits up to `BOOT` for a request at `plane` that `pick` takes. The
/// requests before it are dropped. A wait that runs out fails `step`.
#[cfg(test)]
fn hit(plane: &Receiver<Seen>, step: &str, pick: impl Fn(&Seen) -> bool) -> Seen {
    let deadline = Instant::now() + BOOT;
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        let seen = plane
            .recv_timeout(left)
            .unwrap_or_else(|_| panic!("{step}: no such request in {BOOT:?}"));
        if pick(&seen) {
            return seen;
        }
    }
}

/// Drops each request that `plane` holds now.
#[cfg(test)]
fn drain(plane: &Receiver<Seen>) {
    while plane.try_recv().is_ok() {}
}

/// Holds that `seen` came through the splice of guest `port` with the
/// bearer `secret` of `creds.json`, so the 1100 reply reached the agent
/// (decisions CH5a, CH6-2).
#[cfg(test)]
fn through(seen: &Seen, port: u16, secret: &str, step: &str) {
    assert_eq!(seen.host, format!("127.0.0.1:{port}"), "{step}: {seen:?}");
    assert_eq!(
        seen.authorization,
        format!("Bearer {secret}"),
        "{step}: {seen:?}"
    );
}

/// One ACP link on guest port 1201: `CONNECT 1201` on the vsock socket of
/// CH, as `AcpLink::dial` dials (decision CH13). Guest init joins it to
/// the relay socket of the facade (decision CH62 rule 5).
struct Acp {
    stream: UnixStream,
    reader: BufReader<UnixStream>,
    next: u64,
}

impl Acp {
    #[cfg(test)]
    fn open(vsock: &Path, step: &str) -> Self {
        let stream = UnixStream::connect(vsock).unwrap();
        stream.set_read_timeout(Some(ANSWER)).unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        writeln!(&stream, "CONNECT {}", GuestPort::Acp.number()).unwrap();
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(line.starts_with("OK "), "{step}: CONNECT gave {line:?}");
        Self {
            stream,
            reader,
            next: 0,
        }
    }

    /// Sends the request `method` and reads up to its answer. It gives
    /// each notification before the answer, then the result. An error
    /// answer fails `step`.
    #[cfg(test)]
    fn call(&mut self, method: &str, params: &Value, step: &str) -> (Vec<Value>, Value) {
        self.next += 1;
        let id = self.next;
        let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        writeln!(&self.stream, "{request}").unwrap();
        let mut notes = Vec::new();
        loop {
            let mut line = String::new();
            let read = self.reader.read_line(&mut line).unwrap();
            assert!(
                read > 0,
                "{step}: the link ended before the answer to {method}"
            );
            let message: Value = serde_json::from_str(&line).unwrap();
            // A request of the facade carries a method, and its id is the
            // facade's own, so only a message with no method answers.
            if message.get("method").is_none() && message.get("id") == Some(&json!(id)) {
                assert!(
                    message.get("error").is_none(),
                    "{step}: {method} gave {message}"
                );
                return (notes, message["result"].clone());
            }
            notes.push(message);
        }
    }
}

/// The text of each `agent_message_chunk` of `notes`, joined.
#[cfg(test)]
fn said(notes: &[Value]) -> String {
    notes
        .iter()
        .filter(|note| note["method"] == "session/update")
        .map(|note| &note["params"]["update"])
        .filter(|update| update["sessionUpdate"] == "agent_message_chunk")
        .filter_map(|update| update["content"]["text"].as_str())
        .collect()
}

/// A prompt of one text on `link`. It gives the notifications, the stop
/// reason and the time of the prompt in ms.
#[cfg(test)]
fn prompt(link: &mut Acp, text: &str, step: &str) -> (Vec<Value>, Value, u128) {
    let began = Instant::now();
    let params = json!({"sessionId": SESSION, "prompt": [{"type": "text", "text": text}]});
    let (notes, result) = link.call("session/prompt", &params, step);
    (
        notes,
        result["stopReason"].clone(),
        began.elapsed().as_millis(),
    )
}

/// `initialize` on `link`, as attendance sends it.
#[cfg(test)]
fn initialize(link: &mut Acp, step: &str) {
    let params = json!({
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false},
        "clientInfo": {"name": "kvm-facade", "version": "0.0.0"},
    });
    let (_, result) = link.call("initialize", &params, step);
    assert_eq!(result["protocolVersion"], 1, "{step}: {result}");
}

/// Places the base disk of the setup script in the image root `root`: a
/// hard link, not a copy. The vmm import reads it through `zstd -d` and
/// checks the SHA-256 of the line of `BASE_DIGEST` (decision CH41 rule 1).
#[cfg(test)]
fn base_disk(_work: &Path, root: &Path) -> RootDisk {
    let files = files();
    fs::hard_link(files.join(BASE_DISK), root.join(BASE_DISK)).unwrap();
    let line = fs::read_to_string(files.join(BASE_DIGEST)).unwrap();
    let sha256 = line
        .strip_suffix("  base.ext4\n")
        .filter(|digest| digest.len() == 64)
        .unwrap_or_else(|| panic!("{BASE_DIGEST} is not a digest line of playpen-disk: {line:?}"));
    RootDisk {
        file: BASE_DISK.to_owned(),
        sha256: sha256.to_owned(),
    }
}

/// The facade state of the vmm once `ready` holds for it, within `wait`.
/// A wait that runs out fails `step` and prints the serial log.
#[cfg(test)]
fn facade_until(
    vmm: &mut Vmm,
    session: &SessionDir,
    wait: Duration,
    step: &str,
    ready: impl Fn(&FacadeState) -> bool,
) -> FacadeState {
    let deadline = Instant::now() + wait;
    loop {
        let answer = vmm.ask("facade");
        let facade = answer.strip_prefix("facade ").unwrap();
        if let Ok(state) = serde_json::from_str::<FacadeState>(facade)
            && ready(&state)
        {
            return state;
        }
        if Instant::now() > deadline {
            let serial = lines(&session.serial_log()).join("\n");
            panic!("{step}: the facade is {answer} after {wait:?}. Serial:\n{serial}");
        }
        sleep(Duration::from_millis(200));
    }
}

/// The real facade proof: the seven steps of the phase 3 proof on one VM
/// with the base disk (decisions CH62 rule 12, CH80). Each step names the
/// rows whose idea it needs. The job log holds both prompt times
/// (decision CH23).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_real_facade_prompts_sleeps_restores_and_stops() {
    let (litellm_address, litellm_seen) = stub(litellm);
    let (chaperone_address, chaperone_seen) = stub(chaperone);
    let booted = boot_with(
        "facade",
        base_disk,
        SHAPE,
        (litellm_address, chaperone_address),
        |name, env, _| Vmm::start(name, env),
    );
    let (mut vmm, vsock, session) = (booted.vmm, booted.vsock, booted.session);

    // 1. Cold boot: the start ends after the first ControlReady. Its facade
    // is `starting` with start 1, or already `running` (CH21-8, CH25 rule
    // 2, CH32 rule 6).
    let (_, ready) = vmm.start_vm("step 1 cold boot");
    let (pid, starts) = match &ready {
        FacadeState::Starting(first) => (first.pid, first.starts.get()),
        FacadeState::Running(first) => (first.pid, first.starts.get()),
        other => panic!("step 1 cold boot: the facade is {other:?}"),
    };
    assert_eq!(starts, 1, "step 1 cold boot");

    // 2. The facade pulls its start state on 1100, starts Flue, listens on
    // the relay socket and opens its local link. A facade line then says
    // `running` with the same pid (CH5a, CH32 rule 6, CH62 rules 10 and
    // 13, CH80).
    let running = facade_until(&mut vmm, &session, FACADE_UP, "step 2 running", |state| {
        matches!(state, FacadeState::Running(_))
    });
    let FacadeState::Running(running) = running else {
        panic!("step 2 running: {running:?}");
    };
    assert_eq!(
        (running.pid, running.starts.get()),
        (pid, 1),
        "step 2 running: another facade"
    );
    // The start read the limits of the alias with the key of the pull.
    let info = hit(&litellm_seen, "step 2 limits", |seen| {
        seen.path == "/v1/model/info"
    });
    through(&info, LITELLM_PORT, LITELLM_KEY, "step 2 limits");

    // 3. On 1201: initialize, then session/prompt of the host's id with no
    // session/new, as attendance sends it. The facade opened that session
    // at start (CH21-8, CH25 rule 2). The turn ends with `end_turn` and the
    // stub text. Both stubs got a request through the guest splice with
    // the bearer of creds.json, so the 1100 reply reached the agent (CH5a,
    // CH6-2).
    let mut link = Acp::open(&vsock, "step 3 open");
    initialize(&mut link, "step 3 initialize");
    let (notes, stop, cold_ms) = prompt(&mut link, FIRST_PROMPT, "step 3 prompt");
    assert_eq!(stop, "end_turn", "step 3 prompt: {notes:?}");
    assert!(said(&notes).contains(HELLO), "step 3 prompt: {notes:?}");
    let completed = hit(&litellm_seen, "step 3 LiteLLM", |seen| {
        seen.path.ends_with("/chat/completions")
    });
    through(&completed, LITELLM_PORT, LITELLM_KEY, "step 3 LiteLLM");
    let listed = hit(&chaperone_seen, "step 3 chaperone", |seen| {
        seen.body["method"] == "tools/list"
    });
    through(&listed, CHAPERONE_PORT, PEP_TOKEN, "step 3 chaperone");

    // 4. Sleep: the link closes, guest init has the turn end, then the
    // guest syncs and the VM snapshots into one generation (CH19 rule 6,
    // CH62 rule 23).
    drop(link);
    facade_until(
        &mut vmm,
        &session,
        BOOT,
        "step 4 turn end",
        |state| matches!(state, FacadeState::Running(up) if !up.turn_running && up.last_turn_end.is_some()),
    );
    assert_eq!(vmm.ask("sleep"), "slept 1", "step 4 sleep");
    let generations: Vec<PathBuf> = fs::read_dir(session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 4 sleep: {generations:?}");

    // 5. Restore: the start claims that generation. The ready says
    // `running` with the same pid and start 1, so the same facade answers,
    // and the serial log has no `up` line (CH19 rules 1, 2 and 5).
    let (restore_ms, ready) = vmm.start_vm("step 5 restore");
    assert!(
        generations[0].join(CONSUMED).is_file(),
        "step 5 restore: no claim"
    );
    assert!(
        !lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 5 restore: the VM booted"
    );
    let FacadeState::Running(restored) = ready else {
        panic!("step 5 restore: the ready is {ready:?}");
    };
    assert_eq!(
        (restored.pid, restored.starts.get()),
        (pid, 1),
        "step 5 restore"
    );

    // 6. A fresh 1201 link: session/load replays the first turn from
    // flue.db, then a second prompt calls the stub tool. LiteLLM and the
    // chaperone get requests again through fresh dials, with the bearers
    // of the pull before that prompt (CH5a, CH62 rule 14, fact 74).
    drain(&litellm_seen);
    drain(&chaperone_seen);
    let mut link = Acp::open(&vsock, "step 6 open");
    initialize(&mut link, "step 6 initialize");
    let params = json!({"sessionId": SESSION, "cwd": "/root", "mcpServers": []});
    let (replayed, _) = link.call("session/load", &params, "step 6 session/load");
    assert!(
        said(&replayed).contains(HELLO),
        "step 6 session/load: {replayed:?}"
    );
    let (notes, stop, restored_ms) = prompt(&mut link, SECOND_PROMPT, "step 6 prompt");
    assert_eq!(stop, "end_turn", "step 6 prompt: {notes:?}");
    assert!(
        said(&notes).contains(AFTER_TOOL),
        "step 6 prompt: {notes:?}"
    );
    let completed = hit(&litellm_seen, "step 6 LiteLLM", |seen| {
        seen.path.ends_with("/chat/completions")
    });
    through(&completed, LITELLM_PORT, LITELLM_KEY, "step 6 LiteLLM");
    let called = hit(&chaperone_seen, "step 6 chaperone", |seen| {
        seen.body["method"] == "tools/call"
    });
    through(&called, CHAPERONE_PORT, PEP_TOKEN, "step 6 chaperone");
    assert_eq!(
        called.body["params"]["name"], TOOL,
        "step 6 chaperone: {called:?}"
    );
    drop(link);
    // Decision CH23: the proof measures the prompts. A direct write skips
    // the capture of the test harness, so the job log holds the line.
    let times = format!(
        "real facade proof: prompt {cold_ms} ms cold, {restored_ms} ms after a restore, restore start {restore_ms} ms\n"
    );
    io::stderr().write_all(times.as_bytes()).unwrap();

    // 7. Stop: the same facade is still `running` with no turn after the
    // link closed. It gets the stop line and exits by itself inside the
    // stop wait, guest init remounts the session disk and powers off, and
    // e2fsck finds the disk clean with flue.db on it (CH19 rule 3, CH62
    // rule 9, CH66).
    let live = facade_until(
        &mut vmm,
        &session,
        BOOT,
        "step 7 live",
        |state| matches!(state, FacadeState::Running(up) if !up.turn_running),
    );
    let FacadeState::Running(live) = live else {
        panic!("step 7 live: {live:?}");
    };
    assert_eq!(
        (live.pid, live.starts.get()),
        (pid, 1),
        "step 7 live: another facade"
    );
    let answer = vmm.ask("stop");
    let ms: u64 = answer
        .strip_prefix("stopped ")
        .unwrap_or_else(|| panic!("step 7 stop: {answer}"))
        .parse()
        .unwrap();
    assert!(
        Duration::from_millis(ms) < FACADE_STOP_WAIT,
        "step 7 stop: {ms} ms"
    );
    let serial = lines(&session.serial_log());
    for line in [
        STOP_LINE,
        "guest-init: session remounted ro",
        "guest-init: power off",
    ] {
        assert!(
            serial.iter().any(|each| each == line),
            "step 7 stop: no {line}: {serial:?}"
        );
    }
    let signalled: Vec<&String> = serial
        .iter()
        .filter(|line| line.ends_with(" sent SIGTERM") || line.ends_with(" sent SIGKILL"))
        .collect();
    assert!(signalled.is_empty(), "step 7 stop: {signalled:?}");
    let header = Command::new(DUMPE2FS)
        .arg("-h")
        .arg(session.disk())
        .output()
        .unwrap();
    let header = String::from_utf8_lossy(&header.stdout);
    assert!(
        !header.contains("needs_recovery"),
        "step 7 stop: the journal waits for a replay"
    );
    let check = Command::new(E2FSCK)
        .arg("-fn")
        .arg(session.disk())
        .output()
        .unwrap();
    assert!(check.status.success(), "step 7 stop: e2fsck -fn: {check:?}");
    let stat = Command::new(DEBUGFS)
        .args(["-R", "stat /flue.db"])
        .arg(session.disk())
        .output()
        .unwrap();
    let stat = String::from_utf8_lossy(&stat.stdout);
    assert!(
        stat.contains("Type: regular"),
        "step 7 stop: no flue.db: {stat}"
    );

    drop(vmm);
}
