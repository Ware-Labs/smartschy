"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.PythonRunner = void 0;
const child_process_1 = require("child_process");
class PythonRunner {
    constructor(options) {
        this.cwd = options.cwd;
        this.pythonBinary = options.pythonBinary ?? "python";
    }
    runModule(args, events = {}) {
        const proc = (0, child_process_1.spawn)(this.pythonBinary, args, {
            cwd: this.cwd,
            stdio: ["ignore", "pipe", "pipe"],
            windowsHide: true,
        });
        proc.stdout.setEncoding("utf8");
        proc.stderr.setEncoding("utf8");
        let stdoutBuffer = "";
        let stderrBuffer = "";
        proc.stdout.on("data", (chunk) => {
            stdoutBuffer += chunk;
            stdoutBuffer = this.flushLines(stdoutBuffer, events.onStdout);
        });
        proc.stderr.on("data", (chunk) => {
            stderrBuffer += chunk;
            stderrBuffer = this.flushLines(stderrBuffer, events.onStderr);
        });
        return {
            process: proc,
            cancel: () => {
                proc.kill();
            },
            waitForExit: async () => {
                return new Promise((resolve, reject) => {
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
    flushLines(buffer, onLine) {
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
exports.PythonRunner = PythonRunner;
//# sourceMappingURL=pythonRunner.js.map