export type SupportRef = {
    id: string;
    sourceType: string;
    sourceId: string;
    confidence?: number;
    attributes?: Record<string, unknown>;
    retrievedAt?: string;
};
export type UnitWithSupport<TUnit> = {
    unit: TUnit;
    supportIds: string[];
    supportRefs: SupportRef[];
};
//# sourceMappingURL=support.d.ts.map