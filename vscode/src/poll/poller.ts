import * as vscode from "vscode";
import { PollResult } from "../backend/types";
import { setting } from "../config";
import { DebugState } from "../state/session";
import { ViewPolling } from "../state/viewPolling";
import { Output } from "../ui/output";

/** The 1 Hz heartbeat: `poll` while connected and idle, diff into the state.
 *
 * The backend's poll reads only EDPRSR/EDSCR/DHCSR -- nothing invasive -- and
 * the queue is strictly sequential, so a tick simply skips while any command
 * (or a long op like load) is in flight.
 */
export class Poller implements vscode.Disposable {
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight = false;
  private failures = 0;

  constructor(
    private readonly state: DebugState,
    private readonly output: Output,
    private readonly polling?: ViewPolling,
  ) {}

  start(): void {
    this.stop();
    const interval = setting<number>("pollIntervalMs", 1000);
    if (interval <= 0) {
      return;
    }
    this.timer = setInterval(() => void this.tick(), Math.max(interval, 200));
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  async tick(force = false): Promise<void> {
    if (
      !this.state.connected ||
      this.state.busyOp ||
      this.inFlight ||
      (this.state.client?.busy ?? true) ||
      // The Cores view's Auto-refresh checkbox; the manual refresh button
      // passes force. Pushed stopped events keep arriving either way.
      (!force && this.polling !== undefined && !this.polling.on("cores"))
    ) {
      return;
    }
    this.inFlight = true;
    try {
      const result = (await this.state.client!.run("poll")) as PollResult;
      this.failures = 0;
      this.state.applyPoll(result);
    } catch (err) {
      this.failures += 1;
      if (this.failures === 3) {
        this.output.line(`!! polling failing repeatedly: ${err}`);
      }
      if (this.failures >= 10) {
        this.stop();
        void vscode.window.showWarningMessage(
          "S32E Debug: status polling stopped after repeated failures -- " +
            "see the output log. Reconnect to resume.",
        );
      }
    } finally {
      this.inFlight = false;
    }
  }

  dispose(): void {
    this.stop();
  }
}
