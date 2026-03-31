export interface ExecResult {
    stdout: string;
    stderr: string;
    exitCode: number;
}
export declare class Workspace {
    readonly dir: string;
    constructor(dir: string);
    exec(cmd: string, opts?: {
        throws?: boolean;
    }): ExecResult;
    applyPatch(patchText: string): ExecResult;
    applyPatchForReal(patchText: string): ExecResult;
    reset(): void;
    diff(): string;
    static checkout(repoUrl: string, baseCommit: string, targetDir: string): Workspace;
    static fromLocalPath(dir: string): Workspace;
}
//# sourceMappingURL=workspace.d.ts.map