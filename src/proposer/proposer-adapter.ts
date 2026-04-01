import type { BenchmarkInstance, PatchCandidate } from "../types/contracts.js"
import type { SearchStrategy } from "../types/strategy.js"
import { buildSystemPrompt, buildUserPrompt } from "./prompt-builder.js"
import { parseResponse } from "./response-parser.js"
import { callLLM } from "./llm-client.js"

export async function propose(
  instance: BenchmarkInstance,
  attempt: number,
  opts: {
    previousFeedback?: string
    fileContents?: Record<string, string>
    strategy?: SearchStrategy
  } = {}
): Promise<PatchCandidate> {
  const system = buildSystemPrompt()
  const prompt = buildUserPrompt(instance, opts)

  const result = await callLLM({ system, prompt })
  const candidate = parseResponse(result.content, attempt)

  console.log(
    `  [proposer] attempt=${attempt} in=${result.inputTokens} out=${result.outputTokens} files=${candidate.filesTouched.join(",") || "(none)"}`
  )

  return candidate
}
