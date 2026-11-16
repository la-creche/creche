// The conversation that a rewind kept, as the model reads it. Flue 2.3.0
// cannot cut a conversation ("Appends are linear"), so a rewind starts a
// new Flue instance (`facade/src/rewind.ts`). The new instance has no
// record of the kept messages. The facade gives their text here, and each
// render of that instance puts it in the instructions:
//
//   facade rewind ──► earlier.set(instance, transcript)
//   render of the instance ──► useInstruction(an untrusted frame of the transcript)
//
// The transcript holds tool output, so it goes in one untrusted frame
// (invariant 14). The model reads it as data, never as instruction.

import { wrapUntrusted } from "./untrusted.js";

/** The label of the frame. A constant: it holds no quote that breaks the frame. */
const FRAME_LABEL = "rewind";

const LEAD =
  "The user rewound this conversation. The frame below is the record of the " +
  "conversation before this point: the user's messages, your answers and your " +
  "tool calls. Continue from it. The files of the workspace did not go back, " +
  "so they can differ from what the record says.";

export class Earlier {
  private readonly byInstance = new Map<string, string>();

  /** Sets the kept transcript of the Flue instance `instance`. An empty transcript sets none. */
  public set(instance: string, transcript: string): void {
    if (transcript.length === 0) {
      this.byInstance.delete(instance);
      return;
    }

    this.byInstance.set(instance, transcript);
  }

  /** The instruction of the instance `instance`, or null when a rewind kept nothing for it. */
  public instructionFor(instance: string | null): string | null {
    const transcript = instance === null ? undefined : this.byInstance.get(instance);
    if (transcript === undefined) {
      return null;
    }

    return `${LEAD}\n\n${wrapUntrusted(FRAME_LABEL, transcript)}`;
  }
}
