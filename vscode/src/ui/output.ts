import * as vscode from "vscode";

/** One channel for everything: protocol lines, backend stderr, diagnostics. */
export class Output {
  readonly channel: vscode.OutputChannel;

  constructor() {
    this.channel = vscode.window.createOutputChannel("S32E Debug");
  }

  sent(line: string): void {
    this.channel.appendLine(`>> ${line}`);
  }

  envelope(summary: string): void {
    this.channel.appendLine(`<< ${summary}`);
  }

  stderr(chunk: string): void {
    for (const line of chunk.split(/\r?\n/)) {
      if (line.trim().length > 0) {
        this.channel.appendLine(`   ${line}`);
      }
    }
  }

  section(title: string): void {
    this.channel.appendLine("");
    this.channel.appendLine(`── ${title} ${"─".repeat(Math.max(0, 60 - title.length))}`);
  }

  line(text: string): void {
    this.channel.appendLine(text);
  }

  show(): void {
    this.channel.show(true);
  }

  dispose(): void {
    this.channel.dispose();
  }
}
