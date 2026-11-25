import type { Message } from "./request.js";
import type { ToolDefinition } from "./request.js";
import type { Response } from "./response.js";

export interface Backend {
  complete(messages: Message[], tools?: ToolDefinition[]): Response;
  supportsNativeToolCalling(): boolean;
  modelName(): string;
  contextWindowSize(): number;
}

export interface AsyncBackend {
  complete(messages: Message[], tools?: ToolDefinition[]): Promise<Response>;
  supportsNativeToolCalling(): boolean;
  modelName(): string;
  contextWindowSize(): number;
}
