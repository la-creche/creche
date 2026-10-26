// The beat deadline. attendance sends `_creche/beat` every 30 s. A
// client-side kill of `sbx exec` does not reach the process in the VM, so
// stdin EOF is not the one way the facade ends: a missed deadline ends it.

export class BeatDeadline {
  private timer: NodeJS.Timeout | null = null;

  public constructor(
    private readonly deadlineMs: number,
    private readonly onExpire: () => void,
  ) {}

  /** Arms the deadline, or moves it one whole window on. */
  public feed(): void {
    this.stop();
    this.timer = setTimeout(this.onExpire, this.deadlineMs);
  }

  public stop(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
