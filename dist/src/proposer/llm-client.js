// Minimal Bedrock LLM client — standalone copy, no jingu-agent dependency.
import { BedrockRuntimeClient, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
const DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0";
const DEFAULT_REGION = "us-east-1";
const DEFAULT_MAX_TOKENS = 4096;
let _client = null;
function getClient() {
    if (!_client) {
        _client = new BedrockRuntimeClient({ region: DEFAULT_REGION, maxAttempts: 1 });
    }
    return _client;
}
export async function callLLM(opts) {
    const body = JSON.stringify({
        anthropic_version: "bedrock-2023-05-31",
        max_tokens: opts.maxTokens ?? DEFAULT_MAX_TOKENS,
        temperature: 0,
        system: opts.system,
        messages: [{ role: "user", content: opts.prompt }],
    });
    const command = new InvokeModelCommand({
        modelId: DEFAULT_MODEL,
        contentType: "application/json",
        accept: "application/json",
        body,
    });
    const response = await getClient().send(command);
    const decoded = JSON.parse(new TextDecoder().decode(response.body));
    const content = decoded.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");
    return {
        content,
        inputTokens: decoded.usage?.input_tokens ?? 0,
        outputTokens: decoded.usage?.output_tokens ?? 0,
    };
}
//# sourceMappingURL=llm-client.js.map