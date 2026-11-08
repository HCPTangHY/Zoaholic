export interface HierarchyKey { id?: string; parent_id?: string; api: string; preferences?: Record<string, unknown>; [key: string]: unknown }
export function keyTreeRows<T extends {id?: string; parent_id?: string}>(keys: T[], collapsed?: Set<string>): {key: T; index: number; depth: number; hasChildren: boolean}[];
export function inheritedKeyValue(parent: any, field: string): any;
export function splitKeyQuota(quota: any): { rules: {key: string; value: string}[]; models: {key: string; value: string}[] };
export function ownKeyFields(key: object | null): Set<string>;
export function retainKeyOverrides<T extends object>(target: T, fields: Set<string>): T;
