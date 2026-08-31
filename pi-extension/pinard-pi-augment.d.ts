// Type augmentation for the Pi ExtensionAPI.
//
// Pinard relies on a few runtime-supported capabilities that the published
// @earendil-works/pi-coding-agent types do not model:
//
//  - pi.emit(event, data): a cross-extension event bus. The babysitter emits
//    "babysitter:*" events that the worker subscribes to. Present at runtime
//    on the extension runner; absent from the ExtensionAPI .d.ts.
//  - pi.setStatus(key, text): declared on ExtensionUIContext but also exposed
//    on the API object at runtime (the worker calls it via optional chaining).
//  - on(customEvent, handler): the typed overloads only cover built-in events.
//    Pinard listens to custom event strings ("babysitter:waiting_for_event")
//    and legacy names ("notification", "session_end") that the runtime accepts.
//
// This augmentation makes the type-checker reflect actual runtime behavior so
// `tsc --noEmit` catches real bugs (typos, undefined vars, null-safety) instead
// of drowning in known type-vs-runtime gaps. All additions use optional/loose
// signatures to avoid masking genuine misuse of the typed overloads.

import "@earendil-works/pi-coding-agent";

declare module "@earendil-works/pi-coding-agent" {
  interface ExtensionAPI {
    /** Cross-extension event bus (runtime-only; not in published types). */
    emit?(event: string, data?: unknown): void;
    /** Status-bar setter, also exposed on the API object at runtime. */
    setStatus?(key: string, text: string | undefined): void;
    /** Catch-all for custom/legacy event names the runtime tolerates. */
    on(event: string, handler: (event: any, ctx?: any) => any): void;
  }
}
