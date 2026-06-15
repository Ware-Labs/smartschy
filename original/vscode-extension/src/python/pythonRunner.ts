import { ChildProcess, spawn } from "child_process";

export interface RunnerEvents {
  onStdout?: (line: string) => void;
  onStderr?: (line: string) => void;
}

export interface RunningCommand {
  readonly process: ChildProcess;
  cancel(): void;
  waitForExit(): Promise<{ exitCode: number }>;
}

export interface PythonRunnerOptions {
  cwd: string;
  pythonBinary?: string;
}

export class PythonRunner {
  private readonly cwd: string;
  private readonly pythonBinary: string;

  public constructor(options: PythonRunnerOptions) {
    this.cwd = options.cwd;
    this.pythonBinary = options.pythonBinary ?? "python";
  }

  public runModule(args: string[], events: RunnerEvents = {}): RunningCommand {
    const proc = spawn(this.pythonBinary, args, {
      cwd: this.cwd,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");

    let stdoutBuffer = "";
    let stderrBuffer = "";
    proc.stdout.on("data", (chunk: string) => {
      stdoutBuffer += chunk;
      stdoutBuffer = this.flushLines(stdoutBuffer, events.onStdout);
    });
    proc.stderr.on("data", (chunk: string) => {
      stderrBuffer += chunk;
      stderrBuffer = this.flushLines(stderrBuffer, events.onStderr);
    });

    return {
      process: proc,
      cancel: () => {
        proc.kill();
      },
      waitForExit: async () => {
        return new Promise<{ exitCode: number }>((resolve, reject) => {
          proc.on("error", (err) => reject(err));
          proc.on("close", (exitCode) => {
            if (stdoutBuffer.trim()) {
              events.onStdout?.(stdoutBuffer.trim());
            }
            if (stderrBuffer.trim()) {
              events.onStderr?.(stderrBuffer.trim());
            }
            resolve({ exitCode: exitCode ?? 1 });
          });
        });
      },
    };
  }

  private flushLines(buffer: string, onLine?: (line: string) => void): string {
    const parts = buffer.split(/\r?\n/);
    const trailing = parts.pop() ?? "";
    for (const line of parts) {
      const text = line.trimEnd();
      if (text) {
        onLine?.(text);
      }
    }
    return trailing;
  }
}

