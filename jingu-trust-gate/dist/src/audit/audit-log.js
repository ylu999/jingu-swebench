import { appendFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
export class FileAuditWriter {
    filePath;
    constructor(filePath) {
        this.filePath = filePath;
    }
    async append(entry) {
        // Ensure directory exists
        await mkdir(dirname(this.filePath), { recursive: true });
        // Append one JSON line (newline-delimited JSON)
        const line = JSON.stringify(entry) + "\n";
        await appendFile(this.filePath, line, "utf-8");
    }
}
// Default path: .jingu-trust-gate/audit.jsonl relative to cwd
export function createDefaultAuditWriter() {
    return new FileAuditWriter(".jingu-trust-gate/audit.jsonl");
}
//# sourceMappingURL=audit-log.js.map