export enum MessageType {
  SYSTEM_PROMPT = "system_prompt",
  USER_INPUT = "user_input",
  TOOL_CALL = "tool_call",
  TOOL_RESULT = "tool_result",
  REASONING = "reasoning",
  TEXT_RESPONSE = "text_response",
  STEP_NUDGE = "step_nudge",
  PREREQUISITE_NUDGE = "prerequisite_nudge",
  RETRY_NUDGE = "retry_nudge",
  CONTEXT_WARNING = "context_warning",
  SUMMARY = "summary",
}

export interface MessageMeta {
  type: MessageType;
  step_index?: number;
  original_type?: MessageType;
  token_estimate?: number;
}

export enum Action {
  ALLOW = "allow",
  DENY = "deny",
  FILTER = "filter",
}

export interface CallerContext {
  user_id?: string;
  session_id?: string;
  role?: string;
  source?: string;
  trusted: boolean;
  metadata?: Record<string, string>;
  /** The human who originated the request; carried unchanged across delegation hops. */
  invoking_subject?: string;
  /** Span of the delegating caller, when this call was made by a subagent. */
  parent_span?: string;
  /** 0 for a direct call; incremented at each delegation hop. */
  delegation_depth?: number;
}

export interface Decision {
  action: Action;
  rule: string;
  reason: string;
  redacted_args?: Record<string, unknown>;
  /**
   * Opaque enrollment object from kei-proxy on a DENY (provider,
   * provider_user_id, org_id, workspace_id, and optionally url/expires_at).
   * The url is a one-time claim link: surface it to the user, never log it.
   */
  enrollment?: Record<string, unknown>;
  timestamp: Date;
}

export function createAllowDecision(reason = "allowed"): Decision {
  return {
    action: Action.ALLOW,
    rule: "default",
    reason,
    timestamp: new Date(),
  };
}

export function createDenyDecision(reason: string): Decision {
  return {
    action: Action.DENY,
    rule: "default",
    reason,
    timestamp: new Date(),
  };
}