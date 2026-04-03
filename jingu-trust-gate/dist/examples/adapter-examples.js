/**
 * Reference adapter implementations for jingu-trust-gate.
 *
 * These show how to implement ContextAdapter for Claude, OpenAI, and Gemini.
 * Copy and adapt as needed for your application — they are NOT part of the
 * core SDK.
 *
 * Usage:
 *   import { ClaudeContextAdapter } from "./examples/adapter-examples.js";
 */
/**
 * Converts VerifiedContext → Claude API search_result blocks.
 *
 * Usage:
 *   const adapter = new ClaudeContextAdapter();
 *   const blocks = adapter.adapt(verifiedCtx);
 *   // Pass blocks as tool_result content or top-level user message content
 */
export class ClaudeContextAdapter {
    citations;
    sourcePrefix;
    constructor(options = {}) {
        this.citations = options.citations ?? true;
        this.sourcePrefix = options.sourcePrefix ?? "";
    }
    adapt(context) {
        return context.admittedBlocks.map((block) => this.blockToSearchResult(block));
    }
    blockToSearchResult(block) {
        const textParts = [block.content];
        if (block.grade) {
            textParts.push(`[Evidence grade: ${block.grade}]`);
        }
        if (block.unsupportedAttributes && block.unsupportedAttributes.length > 0) {
            textParts.push(`[Not supported by evidence: ${block.unsupportedAttributes.join(", ")}]`);
        }
        if (block.conflictNote) {
            textParts.push(`[Conflict: ${block.conflictNote}]`);
        }
        return {
            type: "search_result",
            source: `${this.sourcePrefix}${block.sourceId}`,
            title: block.sourceId,
            content: [{ type: "text", text: textParts.join("\n") }],
            citations: { enabled: this.citations },
        };
    }
}
/**
 * Converts VerifiedContext → OpenAI chat message.
 *
 * Usage (tool mode):
 *   const adapter = new OpenAIContextAdapter({ mode: "tool", toolCallId: call.id });
 *   messages.push(adapter.adapt(verifiedCtx));
 *
 * Usage (user mode):
 *   const adapter = new OpenAIContextAdapter();
 *   messages.push(adapter.adapt(verifiedCtx));
 */
export class OpenAIContextAdapter {
    mode;
    toolCallId;
    blockSeparator;
    constructor(options = {}) {
        this.mode = options.mode ?? "user";
        this.toolCallId = options.toolCallId;
        this.blockSeparator = options.blockSeparator ?? "\n\n---\n\n";
    }
    adapt(context) {
        const content = context.admittedBlocks
            .map((block) => this.blockToText(block))
            .join(this.blockSeparator);
        if (this.mode === "tool") {
            return { role: "tool", tool_call_id: this.toolCallId ?? "", content };
        }
        return { role: "user", content };
    }
    blockToText(block) {
        const lines = [`[${block.sourceId}] ${block.content}`];
        if (block.grade)
            lines.push(`Evidence grade: ${block.grade}`);
        if (block.unsupportedAttributes && block.unsupportedAttributes.length > 0) {
            lines.push(`Not supported by evidence: ${block.unsupportedAttributes.join(", ")}`);
        }
        if (block.conflictNote)
            lines.push(`Conflict: ${block.conflictNote}`);
        return lines.join("\n");
    }
}
/**
 * Converts VerifiedContext → Gemini API Content object.
 *
 * Usage:
 *   const adapter = new GeminiContextAdapter();
 *   const content = adapter.adapt(verifiedCtx);
 *   const result = await model.generateContent({
 *     contents: [content, { role: "user", parts: [{ text: userQuery }] }],
 *   });
 */
export class GeminiContextAdapter {
    role;
    constructor(options = {}) {
        this.role = options.role ?? "user";
    }
    adapt(context) {
        if (context.admittedBlocks.length === 0) {
            return { role: this.role, parts: [{ text: "[No verified context available]" }] };
        }
        return {
            role: this.role,
            parts: context.admittedBlocks.map((block) => ({ text: this.blockToText(block) })),
        };
    }
    blockToText(block) {
        const lines = [`[${block.sourceId}] ${block.content}`];
        if (block.grade)
            lines.push(`Evidence grade: ${block.grade}`);
        if (block.unsupportedAttributes && block.unsupportedAttributes.length > 0) {
            lines.push(`Not supported by evidence: ${block.unsupportedAttributes.join(", ")}`);
        }
        if (block.conflictNote)
            lines.push(`Conflict: ${block.conflictNote}`);
        return lines.join("\n");
    }
}
//# sourceMappingURL=adapter-examples.js.map