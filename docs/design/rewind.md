# Rewind a session

> Status as of 2026-11-15: The operator accepted RW1 to RW4 on 2026-11-15. The creche part is built. Panda shows the control in a separate change.

Date: 2026-11-15. The operator asked for a way to rewind a chat from Panda.

## 1. Facts

1. ACP has no method that rewinds, cuts or edits a session.
   - `session/fork` is an unstable draft. It copies a whole session. It has no field that names a message.
   - A `messageId` on `user_message_chunk` is a stable, optional field of ACP v1.
2. Flue 2.3.0 cannot cut a conversation. Its records make a tree, but its reducer refuses a branch: "Appends are linear." The public handle has only `dispatch`, `read` and `abort`.
3. One VM serves one session. The host has no disk copy of a session.
4. No VM takes a snapshot at each turn. So the workspace files cannot go back to an earlier turn.

## 2. Decisions

| Row | Decision |
|---|---|
| RW1 | A rewind cuts the session back to before one user message. Panda puts the text of that message in the composer. The user changes the text or sends it again. |
| RW2 | A rewind cuts the same session, in its own VM. The session id stays. The old messages stay in `flue.db`, and no client sees them. |
| RW3 | The workspace files stay as they are. Panda tells the user before the rewind. |
| RW4 | The wire is the creche extension `_creche/session/rewind`. `facade/vectors/creche-extensions.json` holds its values. |

## 3. The wire

1. The client sends the request `_creche/session/rewind` with `{"sessionId", "messageId"}`.
2. `messageId` is the `messageId` of a `user_message_chunk`. The facade gives it on the live echo of each prompt and on each replay.
3. The facade answers `{}`.
4. A running prompt gets `invalidRequest` (-32600). A `messageId` of no user message gets `invalidParams` (-32602). An unknown session gets `resourceNotFound` (-32002).
5. The door lists the capability `"creche/rewind": {}` in the `_meta` of `agentCapabilities` in its `initialize` answer. The door routes the request by its `sessionId`, as each other session method.

## 4. How the facade cuts

Flue cannot cut, so the facade starts a new Flue instance in the same VM:

1. The facade holds the session, so no prompt starts.
2. It reads the messages of the instance. It puts the kept messages of the last rewind before them.
3. It finds the user message `messageId`, and keeps each message before it.
4. It writes `lineage.json` beside `flue.db`: the generation and the kept messages. A rename makes the write atomic.
5. The session moves to the instance `<session id>-r<generation>`.
6. The agent gets the text of the kept messages. Each render of the new instance puts that text in the instructions, inside one untrusted frame (invariant 14).

`session/load` replays the kept messages, then the history of the instance. At a start, the facade reads `lineage.json`. A file that does not read refuses the start.

The model reads the kept messages as text. It reads an earlier tool call as text too, not as a structured call.

## 5. Release order

The facade must serve the method before the door lists it. Release the guest image with the new facade first. Then release attendance.

## 6. Rejected

| Choice | Why not |
|---|---|
| A fork into a new session | It needs a disk copy in the vmm and a move of the history between two VMs. It is two to three times the work. |
| A direct write of Flue records | It writes the internals of Flue under its producer fence. A new Flue can break it. |
| A Panda change only | A new session gets an empty workspace and a bad title. The old messages show as one user message. |
