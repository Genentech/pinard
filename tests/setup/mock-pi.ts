import { vi } from "vitest";

export interface DeliveredMessage {
  text: string;
  options: { deliverAs?: string };
  timestamp: number;
}

export interface MockPi {
  messages: DeliveredMessage[];
  registeredTools: Map<string, any>;
  eventHandlers: Map<string, Function[]>;
  commands: Map<string, any>;
  statusUpdates: Map<string, string>;

  sendUserMessage(msg: string, opts?: { deliverAs?: string }): void;
  registerTool(tool: any): void;
  on(event: string, handler: Function): void;
  setSessionName(name: string): void;
  setStatus(key: string, value: string): void;
  sendMessage(msg: any, opts?: any): void;
  registerCommand(name: string, config: any): void;
  registerMessageRenderer(name: string, renderer: any): void;
  registerProvider(name: string, config: any): void;

  waitForMessage(
    predicate: (m: DeliveredMessage) => boolean,
    timeout?: number
  ): Promise<DeliveredMessage>;
  getMessagesMatching(pattern: RegExp): DeliveredMessage[];
  clear(): void;
  triggerEvent(event: string, ...args: any[]): Promise<void>;
}

export function createMockPi(): MockPi {
  const messages: DeliveredMessage[] = [];
  const registeredTools = new Map<string, any>();
  const eventHandlers = new Map<string, Function[]>();
  const commands = new Map<string, any>();
  const statusUpdates = new Map<string, string>();
  let messageListeners: Array<(msg: DeliveredMessage) => void> = [];

  const mockPi: MockPi = {
    messages,
    registeredTools,
    eventHandlers,
    commands,
    statusUpdates,

    sendUserMessage: vi.fn((msg: string, opts?: { deliverAs?: string }) => {
      const delivered: DeliveredMessage = {
        text: msg,
        options: opts || {},
        timestamp: Date.now(),
      };
      messages.push(delivered);
      messageListeners.forEach((l) => l(delivered));
    }),

    registerTool: vi.fn((tool: any) => {
      registeredTools.set(tool.name, tool);
    }),

    on: vi.fn((event: string, handler: Function) => {
      const handlers = eventHandlers.get(event) || [];
      handlers.push(handler);
      eventHandlers.set(event, handlers);
    }),

    setSessionName: vi.fn(),
    setStatus: vi.fn((key: string, value: string) => {
      statusUpdates.set(key, value);
    }),
    sendMessage: vi.fn(),
    registerCommand: vi.fn((name: string, config: any) => {
      commands.set(name, config);
    }),
    registerMessageRenderer: vi.fn(),
    registerProvider: vi.fn(),

    async waitForMessage(
      predicate: (m: DeliveredMessage) => boolean,
      timeout = 5000
    ): Promise<DeliveredMessage> {
      const existing = messages.find(predicate);
      if (existing) return existing;

      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          const idx = messageListeners.indexOf(listener);
          if (idx >= 0) messageListeners.splice(idx, 1);
          reject(
            new Error(
              `Timed out waiting for message (${timeout}ms). Got ${messages.length} messages: ${messages.map((m) => m.text.slice(0, 60)).join("; ")}`
            )
          );
        }, timeout);

        const listener = (msg: DeliveredMessage) => {
          if (predicate(msg)) {
            clearTimeout(timer);
            const idx = messageListeners.indexOf(listener);
            if (idx >= 0) messageListeners.splice(idx, 1);
            resolve(msg);
          }
        };
        messageListeners.push(listener);
      });
    },

    getMessagesMatching(pattern: RegExp): DeliveredMessage[] {
      return messages.filter((m) => pattern.test(m.text));
    },

    clear() {
      messages.length = 0;
      messageListeners = [];
      statusUpdates.clear();
    },

    async triggerEvent(event: string, ...args: any[]) {
      const handlers = eventHandlers.get(event) || [];
      for (const handler of handlers) {
        await handler(...args);
      }
    },
  };

  return mockPi;
}
