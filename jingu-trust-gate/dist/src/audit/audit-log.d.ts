import type { AuditEntry, AuditWriter } from "../types/audit.js";
export declare class FileAuditWriter implements AuditWriter {
    private readonly filePath;
    constructor(filePath: string);
    append(entry: AuditEntry): Promise<void>;
}
export declare function createDefaultAuditWriter(): FileAuditWriter;
//# sourceMappingURL=audit-log.d.ts.map