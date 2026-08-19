/** The minimum of the vscode API the tested modules touch at runtime.
 * Wired in as a module alias by vitest.config.ts; type-checking still runs
 * against the real @types/vscode. */

export class EventEmitter<T> {
  private listeners: ((e: T) => void)[] = [];
  event = (listener: (e: T) => void): { dispose(): void } => {
    this.listeners.push(listener);
    return {
      dispose: () => {
        this.listeners = this.listeners.filter((l) => l !== listener);
      },
    };
  };
  fire(value: T): void {
    for (const l of [...this.listeners]) {
      l(value);
    }
  }
  dispose(): void {
    this.listeners = [];
  }
}

export class TreeItem {
  id?: string;
  description?: string;
  tooltip?: string;
  contextValue?: string;
  checkboxState?: number;
  constructor(
    public label?: string,
    public collapsibleState?: number,
  ) {}
}

export enum TreeItemCheckboxState {
  Unchecked = 0,
  Checked = 1,
}

export enum TreeItemCollapsibleState {
  None = 0,
  Collapsed = 1,
  Expanded = 2,
}

export class ThemeIcon {
  constructor(
    public id: string,
    public color?: unknown,
  ) {}
}

export class ThemeColor {
  constructor(public id: string) {}
}

/** Configuration store the tests can seed: workspace.getConfiguration("s32e")
 * reads from `configValues` keyed "s32e.<key>", falling back like the API. */
export const configValues = new Map<string, unknown>();

export const workspace = {
  getConfiguration(section?: string) {
    return {
      get<T>(key: string, fallback: T): T {
        const full = section ? `${section}.${key}` : key;
        return configValues.has(full) ? (configValues.get(full) as T) : fallback;
      },
      async update(): Promise<void> {
        /* not exercised in unit tests */
      },
    };
  },
};
