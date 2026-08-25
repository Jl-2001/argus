/** A minimal, controllable stand-in for the browser's native
 * `EventSource` -- jsdom doesn't implement one. Install with
 * `vi.stubGlobal("EventSource", MockEventSource)` before rendering
 * anything that calls `new EventSource(...)`, then drive it from the
 * test via `simulateOpen`/`simulateError`/`dispatch`. */
export class MockEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  /** Every instance ever constructed, in creation order -- tests grab
   * `MockEventSource.instances.at(-1)!` for "the one the app just
   * created" without needing to intercept the constructor themselves. */
  static instances: MockEventSource[] = [];

  readonly url: string;
  readyState = MockEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  private listeners = new Map<string, Set<(event: MessageEvent) => void>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (event: MessageEvent) => void): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(handler);
  }

  removeEventListener(type: string, handler: (event: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(handler);
  }

  close(): void {
    this.closed = true;
    this.readyState = MockEventSource.CLOSED;
  }

  // -- test-only helpers, not part of the real EventSource API --

  simulateOpen(): void {
    this.readyState = MockEventSource.OPEN;
    this.onopen?.();
  }

  simulateError(readyStateAfter: number = MockEventSource.CONNECTING): void {
    this.readyState = readyStateAfter;
    this.onerror?.();
  }

  dispatch(type: string, data: string): void {
    const event = { data } as MessageEvent;
    for (const handler of this.listeners.get(type) ?? []) handler(event);
  }
}
