import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs"
import { join } from "node:path"
import type { BenchmarkInstance } from "./instance-types.js"

// HuggingFace datasets-server REST API — no Python/huggingface_hub needed
const HF_API_BASE = "https://datasets-server.huggingface.co"
const DATASET_MAP = {
  lite: "SWE-bench/SWE-bench_Lite",
  verified: "SWE-bench/SWE-bench_Verified",
}
const CACHE_DIR = ".cache"

// Raw row shape from HF datasets-server
interface HFRow {
  row: {
    instance_id: string
    repo: string
    base_commit: string
    problem_statement: string
    hints_text?: string
  }
}

interface HFResponse {
  rows: HFRow[]
}

const HF_MAX_PAGE = 100

async function fetchPage(dataset: string, offset: number, length: number): Promise<BenchmarkInstance[]> {
  const url = `${HF_API_BASE}/rows?dataset=${encodeURIComponent(dataset)}&config=default&split=test&offset=${offset}&length=${length}`
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`HuggingFace API error: ${res.status} ${res.statusText}\nURL: ${url}`)
  }
  const data = (await res.json()) as HFResponse
  return data.rows.map((r) => ({
    instanceId: r.row.instance_id,
    repo: r.row.repo,
    baseCommit: r.row.base_commit,
    problemStatement: r.row.problem_statement,
    hintsText: r.row.hints_text ?? undefined,
  }))
}

async function fetchFromHF(dataset: string, n: number): Promise<BenchmarkInstance[]> {
  console.log(`[loader] fetching ${n} instances from HuggingFace...`)
  const results: BenchmarkInstance[] = []
  let offset = 0
  while (results.length < n) {
    const pageSize = Math.min(HF_MAX_PAGE, n - results.length)
    const page = await fetchPage(dataset, offset, pageSize)
    results.push(...page)
    if (page.length < pageSize) break  // no more data
    offset += page.length
  }
  return results
}

export async function loadInstances(opts: {
  dataset: "lite" | "verified"
  n?: number
  noCache?: boolean
}): Promise<BenchmarkInstance[]> {
  const n = opts.n ?? 20
  const dataset = DATASET_MAP[opts.dataset]
  const cacheFile = join(CACHE_DIR, `${opts.dataset}-${n}.json`)

  if (!opts.noCache && existsSync(cacheFile)) {
    console.log(`[loader] using cache: ${cacheFile}`)
    return JSON.parse(readFileSync(cacheFile, "utf8")) as BenchmarkInstance[]
  }

  const instances = await fetchFromHF(dataset, n)
  mkdirSync(CACHE_DIR, { recursive: true })
  writeFileSync(cacheFile, JSON.stringify(instances, null, 2), "utf8")
  console.log(`[loader] cached ${instances.length} instances → ${cacheFile}`)
  return instances
}
