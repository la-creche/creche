//! The one handler of the example: it reads the whole JSON body, answers
//! 101, then writes the body back on the raw stream and echoes each byte
//! that follows. `tests/upgrade_body.rs` uses it too.

use std::future::poll_fn;
use std::io;
use std::pin::Pin;

use creche_vmm::server::Reply;
use creche_wire::{AcpRoute, UpgradeRoute};
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::header::{CONNECTION, HeaderValue, UPGRADE};
use hyper::upgrade::Upgraded;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

/// Reads the body before it answers 101, as the `acp` route must (decision
/// CH20). A body that fails to read gets 400.
pub async fn answer(mut request: Request<Incoming>) -> Reply {
    let upgrade = hyper::upgrade::on(&mut request);
    let mut reply = Response::new(Full::new(Bytes::new()));
    let Ok(body) = request.into_body().collect().await else {
        *reply.status_mut() = StatusCode::BAD_REQUEST;
        return reply;
    };
    let body = body.to_bytes();
    tokio::spawn(async move {
        if let Ok(upgraded) = upgrade.await {
            // The client ends the stream. Its error is no fault here.
            let _ = echo(TokioIo::new(upgraded), &body).await;
        }
    });
    *reply.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    let headers = reply.headers_mut();
    headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
    headers.insert(UPGRADE, HeaderValue::from_static(AcpRoute::TOKEN));
    reply
}

/// Writes `body`, then writes back each byte it reads until the end.
async fn echo(mut stream: TokioIo<Upgraded>, body: &[u8]) -> io::Result<()> {
    write_all(&mut stream, body).await?;
    let mut buffer = [0_u8; 4096];
    loop {
        let mut read = ReadBuf::new(&mut buffer);
        poll_fn(|context| Pin::new(&mut stream).poll_read(context, &mut read)).await?;
        if read.filled().is_empty() {
            return Ok(());
        }
        write_all(&mut stream, read.filled()).await?;
    }
}

async fn write_all(stream: &mut TokioIo<Upgraded>, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        let written = poll_fn(|context| Pin::new(&mut *stream).poll_write(context, bytes)).await?;
        if written == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        bytes = bytes.get(written..).unwrap_or_default();
    }
    poll_fn(|context| Pin::new(&mut *stream).poll_flush(context)).await
}
