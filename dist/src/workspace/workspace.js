import { execSync } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
export class Workspace {
    dir;
    constructor(dir) {
        this.dir = dir;
    }
    exec(cmd, opts = {}) {
        const execOpts = {
            cwd: this.dir,
            encoding: "utf8",
            stdio: ["pipe", "pipe", "pipe"],
        };
        try {
            const stdout = execSync(cmd, execOpts);
            return { stdout: stdout ?? "", stderr: "", exitCode: 0 };
        }
        catch (err) {
            const e = err;
            const result = {
                stdout: e.stdout ?? "",
                stderr: e.stderr ?? "",
                exitCode: e.status ?? 1,
            };
            if (opts.throws)
                throw new Error(`Command failed: ${cmd}\n${result.stderr}`);
            return result;
        }
    }
    applyPatch(patchText) {
        const patchFile = join(this.dir, ".jingu-patch.diff");
        writeFileSync(patchFile, patchText, "utf8");
        return this.exec(`git apply --check "${patchFile}"`);
    }
    applyPatchForReal(patchText) {
        const patchFile = join(this.dir, ".jingu-patch.diff");
        writeFileSync(patchFile, patchText, "utf8");
        return this.exec(`git apply "${patchFile}"`);
    }
    reset() {
        this.exec("git checkout -- .");
        this.exec("git clean -fd");
    }
    diff() {
        return this.exec("git diff").stdout;
    }
    static checkout(repoUrl, baseCommit, targetDir) {
        mkdirSync(targetDir, { recursive: true });
        const ws = new Workspace(targetDir);
        if (!existsSync(join(targetDir, ".git"))) {
            execSync(`git clone ${repoUrl} "${targetDir}"`, { stdio: "pipe" });
        }
        ws.exec(`git checkout ${baseCommit}`, { throws: true });
        return ws;
    }
    // For Day 1: create a fake workspace from local path (no clone needed)
    static fromLocalPath(dir) {
        if (!existsSync(dir)) {
            mkdirSync(dir, { recursive: true });
            execSync("git init", { cwd: dir, stdio: "pipe" });
            execSync('git commit --allow-empty -m "init"', { cwd: dir, stdio: "pipe" });
        }
        return new Workspace(dir);
    }
}
//# sourceMappingURL=workspace.js.map