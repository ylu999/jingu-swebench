import { execSync, type ExecSyncOptions } from "node:child_process"
import { mkdirSync, writeFileSync, existsSync } from "node:fs"
import { join } from "node:path"

export interface ExecResult {
  stdout: string
  stderr: string
  exitCode: number
}

export class Workspace {
  readonly dir: string

  constructor(dir: string) {
    this.dir = dir
  }

  exec(cmd: string, opts: { throws?: boolean } = {}): ExecResult {
    const execOpts: ExecSyncOptions = {
      cwd: this.dir,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
    }
    try {
      const stdout = execSync(cmd, execOpts) as unknown as string
      return { stdout: stdout ?? "", stderr: "", exitCode: 0 }
    } catch (err: unknown) {
      const e = err as { stdout?: string; stderr?: string; status?: number }
      const result: ExecResult = {
        stdout: e.stdout ?? "",
        stderr: e.stderr ?? "",
        exitCode: e.status ?? 1,
      }
      if (opts.throws) throw new Error(`Command failed: ${cmd}\n${result.stderr}`)
      return result
    }
  }

  applyPatch(patchText: string): ExecResult {
    const patchFile = join(this.dir, ".jingu-patch.diff")
    writeFileSync(patchFile, patchText, "utf8")
    return this.exec(`git apply --check "${patchFile}"`)
  }

  applyPatchForReal(patchText: string): ExecResult {
    const patchFile = join(this.dir, ".jingu-patch.diff")
    writeFileSync(patchFile, patchText, "utf8")
    return this.exec(`git apply "${patchFile}"`)
  }

  reset(): void {
    this.exec("git checkout -- .")
    this.exec("git clean -fd")
  }

  diff(): string {
    return this.exec("git diff").stdout
  }

  static checkout(repoUrl: string, baseCommit: string, targetDir: string): Workspace {
    mkdirSync(targetDir, { recursive: true })
    const ws = new Workspace(targetDir)

    if (!existsSync(join(targetDir, ".git"))) {
      execSync(`git clone ${repoUrl} "${targetDir}"`, { stdio: "pipe" })
    }
    ws.exec(`git checkout ${baseCommit}`, { throws: true })
    return ws
  }

  // For Day 1: create a fake workspace from local path (no clone needed)
  static fromLocalPath(dir: string): Workspace {
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true })
      execSync("git init", { cwd: dir, stdio: "pipe" })
      execSync('git commit --allow-empty -m "init"', { cwd: dir, stdio: "pipe" })
    }
    return new Workspace(dir)
  }
}
