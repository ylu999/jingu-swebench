// Mock loader — returns hardcoded instances for Day 1.
// Day 3: replace with HuggingFace JSONL loader.
const MOCK_INSTANCES = [
    {
        instanceId: "django__django-11099",
        repo: "django/django",
        baseCommit: "a2e2ecb9839a95b6f41cc0bcb46e8ba8fc01d7f8",
        problemStatement: "Auth.authenticate() should return None instead of raising PermissionDenied when there are no backends configured.",
        hintsText: "Check django/contrib/auth/__init__.py authenticate()",
    },
];
export function loadInstances(opts) {
    // TODO Day 3: load from HuggingFace datasets-server or local JSONL
    const instances = MOCK_INSTANCES;
    return opts.n != null ? instances.slice(0, opts.n) : instances;
}
//# sourceMappingURL=swebench-loader.js.map