import { validateRPP } from "jingu-protocol";
export function runRPPGate(record, options) {
    if (record == null) {
        return {
            allow: false,
            rpp_status: "missing",
            failures: [{
                    code: "MISSING_STAGE",
                    detail: "No RPP record provided — output must include a ```json rpp block"
                }],
            warnings: [],
        };
    }
    const result = validateRPP(record, options?.policy);
    // Run extra checks — their failures are hard by default
    const extraFailures = [];
    for (const check of options?.extraChecks ?? []) {
        extraFailures.push(...check(record, options?.context));
    }
    const allFailures = [...result.failures, ...extraFailures];
    const allWarnings = [...result.warnings];
    return {
        allow: allFailures.length === 0,
        rpp_status: allFailures.length > 0 ? "invalid" : allWarnings.length > 0 ? "weakly_supported" : "valid",
        failures: allFailures,
        warnings: allWarnings,
    };
}
//# sourceMappingURL=rpp-gate.js.map