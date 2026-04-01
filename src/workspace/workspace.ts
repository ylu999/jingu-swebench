import { execSync, type ExecSyncOptions } from "node:child_process"
import { mkdirSync, writeFileSync, existsSync } from "node:fs"
import { join, resolve } from "node:path"

export interface ExecResult {
  stdout: string
  stderr: string
  exitCode: number
}

export class Workspace {
  readonly dir: string

  constructor(dir: string) {
    this.dir = resolve(dir)  // always absolute
  }

  exec(cmd: string, opts: { throws?: boolean } = {}): ExecResult {
    const execOpts: ExecSyncOptions = {
      cwd: this.dir,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
      // PYTHONPATH=. ensures the repo's own source is importable for dev installs (django, astropy etc.)
      env: { ...process.env, PYTHONPATH: this.dir },
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

  private writePatchFile(patchText: string): string {
    const patchFile = join(this.dir, ".jingu-patch.diff")
    // Ensure patch ends with newline — git apply requires it
    const text = patchText.endsWith("\n") ? patchText : patchText + "\n"
    writeFileSync(patchFile, text, "utf8")
    return patchFile
  }

  applyPatch(patchText: string, fuzz = 0): ExecResult {
    const patchFile = this.writePatchFile(patchText)
    return this.exec(`patch --dry-run -p1 --fuzz=${fuzz} -i "${patchFile}"`)
  }

  applyPatchForReal(patchText: string, fuzz = 5): ExecResult {
    const patchFile = this.writePatchFile(patchText)
    return this.exec(`patch -p1 --fuzz=${fuzz} -i "${patchFile}"`)
  }

  reset(): void {
    this.exec("git checkout -f HEAD")
    this.exec("git clean -fdx")
  }

  removeWorktree(cacheDir: string): void {
    // Remove this worktree from the cache repo's worktree list
    const cache = new Workspace(cacheDir)
    cache.exec(`git worktree remove --force "${this.dir}" 2>/dev/null || true`)
  }

  diff(): string {
    return this.exec("git diff").stdout
  }

  static checkout(repoUrl: string, baseCommit: string, targetDir: string): Workspace {
    mkdirSync(targetDir, { recursive: true })
    const ws = new Workspace(targetDir)

    if (!existsSync(join(targetDir, ".git"))) {
      // Clone with full history so any commit is reachable
      execSync(`git clone --no-local "${repoUrl}" "${targetDir}"`, {
        stdio: "pipe",
        timeout: 300_000,
      })
    }
    // Fetch the specific commit in case it's not on the default branch
    ws.exec(`git fetch origin ${baseCommit} 2>/dev/null || true`)
    ws.exec(`git checkout ${baseCommit}`, { throws: true })
    return ws
  }

  // Clone once into a shared cache dir, then use git worktree for each strategy workspace.
  // git worktree shares the .git object store — no file copying, near-instant setup.
  static checkoutFromCache(repoUrl: string, baseCommit: string, targetDir: string, cacheDir: string): Workspace {
    // Step 1: ensure cache repo exists (clone once)
    if (!existsSync(join(cacheDir, ".git"))) {
      mkdirSync(cacheDir, { recursive: true })
      execSync(`git clone --no-local "${repoUrl}" "${cacheDir}"`, {
        stdio: "pipe",
        timeout: 300_000,
      })
      const cache = new Workspace(cacheDir)
      cache.exec(`git fetch origin ${baseCommit} 2>/dev/null || true`)
      cache.exec(`git checkout ${baseCommit}`, { throws: true })
      cache.reset()
    } else {
      // Cache exists — ensure the commit is available
      const cache = new Workspace(cacheDir)
      cache.exec(`git fetch origin ${baseCommit} 2>/dev/null || true`)
    }

    // Step 2: create worktree (shares .git objects, only creates working tree)
    const absTargetDir = resolve(targetDir)
    if (!existsSync(absTargetDir)) {
      const cache = new Workspace(cacheDir)
      // Ensure parent directory exists
      mkdirSync(absTargetDir.split("/").slice(0, -1).join("/"), { recursive: true })
      // Create a detached worktree at the target commit (use absolute path — cache.exec runs in cacheDir)
      cache.exec(`git worktree add --detach "${absTargetDir}" ${baseCommit}`, { throws: true })
    }

    const ws = new Workspace(targetDir)
    ws.reset()
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
