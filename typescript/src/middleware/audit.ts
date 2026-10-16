import type { Decision } from "./types.js";
import { Action } from "./types.js";

export interface AuditRecord {
  session_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  decision: Decision;
  timestamp: Date;
  invoking_subject?: string;
  parent_span?: string;
  delegation_depth?: number;
  framework?: string;
}

export interface AuditFilter {
  session_id?: string;
  tool_name?: string;
  action?: Action;
  since?: Date;
  limit?: number;
}

export interface Auditor {
  record(record: AuditRecord): void;
  query(filter: AuditFilter): AuditRecord[];
}

export class InMemoryAuditor implements Auditor {
  private records: AuditRecord[] = [];

  record(record: AuditRecord): void {
    this.records.push(record);
  }

  query(filter: AuditFilter): AuditRecord[] {
    const results: AuditRecord[] = [];
    for (const r of this.records) {
      if (filter.session_id && r.session_id !== filter.session_id) continue;
      if (filter.tool_name && r.tool_name !== filter.tool_name) continue;
      if (filter.action && r.decision.action !== filter.action) continue;
      if (filter.since && r.timestamp < filter.since) continue;
      results.push(r);
      if (filter.limit && results.length >= filter.limit) break;
    }
    return results;
  }

  clear(): void {
    this.records = [];
  }
}
