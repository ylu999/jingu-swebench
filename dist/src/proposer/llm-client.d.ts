export interface LLMCallOptions {
    system: string;
    prompt: string;
    maxTokens?: number;
}
export interface LLMCallResult {
    content: string;
    inputTokens: number;
    outputTokens: number;
}
export declare function callLLM(opts: LLMCallOptions): Promise<LLMCallResult>;
//# sourceMappingURL=llm-client.d.ts.map