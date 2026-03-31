import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"

// SWE-bench predictions format: one JSON object per line
// { "instance_id": "...", "model_patch": "...", "model_name_or_path": "..." }
export function writePrediction(
  outPath: string,
  instanceId: string,
  patchText: string,
  modelName = "jingu-swebench"
): void {
  mkdirSync(dirname(outPath), { recursive: true })
  const line = JSON.stringify({ instance_id: instanceId, model_patch: patchText, model_name_or_path: modelName })
  appendFileSync(outPath, line + "\n", "utf8")
}
