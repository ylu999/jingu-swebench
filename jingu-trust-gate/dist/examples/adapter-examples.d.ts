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
import type { ContextAdapter } from "../src/index.js";
import type { VerifiedContext } from "../src/types/renderer.js";
/**
 * Claude API search_result block shape.
 * Matches Anthropic SDK SearchResultBlockParam.
 */
export type ClaudeSearchResultBlock = {
    type: "search_result";
    source: string;
    title: string;
    content: Array<{
        type: "text";
        text: string;
    }>;
    citations?: {
        enabled: boolean;
    };
};
export type ClaudeAdapterOptions = {
    /** Whether to enable Claude's citation feature. Default: true. */
    citations?: boolean;
    /** Prefix for the source identifier. Default: none. */
    sourcePrefix?: string;
};
/**
 * Converts VerifiedContext → Claude API search_result blocks.
 *
 * Usage:
 *   const adapter = new ClaudeContextAdapter();
 *   const blocks = adapter.adapt(verifiedCtx);
 *   // Pass blocks as tool_result content or top-level user message content
 */
export declare class ClaudeContextAdapter implements ContextAdapter<ClaudeSearchResultBlock[]> {
    private readonly citations;
    private readonly sourcePrefix;
    constructor(options?: ClaudeAdapterOptions);
    adapt(context: VerifiedContext): ClaudeSearchResultBlock[];
    private blockToSearchResult;
}
/** OpenAI chat message — tool or user role. */
export type OpenAIChatMessage = {
    role: "tool" | "user";
    content: string;
    tool_call_id?: string;
};
export type OpenAIAdapterOptions = {
    /**
     * "tool"  — wrap as a tool result message (requires toolCallId).
     * "user"  — inject as a user-role message.
     * Default: "user"
     */
    mode?: "tool" | "user";
    toolCallId?: string;
    blockSeparator?: string;
};
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
export declare class OpenAIContextAdapter implements ContextAdapter<OpenAIChatMessage> {
    private readonly mode;
    private readonly toolCallId;
    private readonly blockSeparator;
    constructor(options?: OpenAIAdapterOptions);
    adapt(context: VerifiedContext): OpenAIChatMessage;
    private blockToText;
}
export type GeminiTextPart = {
    text: string;
};
/** Gemini API Content object (one turn in the conversation). */
export type GeminiContent = {
    role: "user" | "model" | "function";
    parts: GeminiTextPart[];
};
export type GeminiAdapterOptions = {
    /** Default: "user" */
    role?: "user" | "function";
};
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
export declare class GeminiContextAdapter implements ContextAdapter<GeminiContent> {
    private readonly role;
    constructor(options?: GeminiAdapterOptions);
    adapt(context: VerifiedContext): GeminiContent;
    private blockToText;
}
//# sourceMappingURL=adapter-examples.d.ts.map