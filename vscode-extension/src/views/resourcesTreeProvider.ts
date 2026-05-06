import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { UnmatchedPart, WorkflowState } from "../state/workflowState";

class ResourceTreeItem extends vscode.TreeItem {
  public constructor(label: string, collapsibleState: vscode.TreeItemCollapsibleState) {
    super(label, collapsibleState);
  }
}

export class ResourcesTreeProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly emitter = new vscode.EventEmitter<void>();
  private state: WorkflowState;
  private readonly workspaceRoot: string;

  public readonly onDidChangeTreeData = this.emitter.event;

  public constructor(initialState: WorkflowState, workspaceRoot: string) {
    this.state = initialState;
    this.workspaceRoot = workspaceRoot;
  }

  public updateState(state: WorkflowState): void {
    this.state = state;
    this.emitter.fire();
  }

  public getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  public getChildren(element?: vscode.TreeItem): vscode.ProviderResult<vscode.TreeItem[]> {
    if (!element) {
      const summary = new ResourceTreeItem(`Workflow: ${this.state.status}`, vscode.TreeItemCollapsibleState.None);
      summary.description = this.state.lastError ? "error" : "ok";
      summary.contextValue = "workflow";

      const actions = new ResourceTreeItem("Actions", vscode.TreeItemCollapsibleState.Expanded);
      actions.contextValue = "actions-group";
      const core = new ResourceTreeItem("Core Inputs", vscode.TreeItemCollapsibleState.Expanded);
      core.contextValue = "core-group";
      const datasheets = new ResourceTreeItem(
        `Datasheets (${this.state.resources.datasheetMappings.length})`,
        vscode.TreeItemCollapsibleState.Expanded
      );
      datasheets.contextValue = "datasheets-group";
      const unmatched = new ResourceTreeItem(
        `Parts Without Datasheets (${this.state.resources.unmatchedParts.length})`,
        vscode.TreeItemCollapsibleState.Expanded
      );
      unmatched.contextValue = "unmatched-group";
      const responses = new ResourceTreeItem(
        `Responses (${this.listResponseFiles().length})`,
        vscode.TreeItemCollapsibleState.Expanded
      );
      responses.contextValue = "responses-group";
      return [summary, actions, core, datasheets, unmatched, responses];
    }

    if (element.label === "Actions") {
      return [
        this.actionItem("Add DSN", "smartschy.addDsn"),
        this.actionItem("Add BOM", "smartschy.addBom"),
        this.actionItem("Add Schematic PDF", "smartschy.addSchematic"),
        this.actionItem("Add Datasheets (Bulk)", "smartschy.addDatasheets"),
        this.actionItem("Ingest", "smartschy.runIngest"),
        this.actionItem("Open Schematic Viewer", "smartschy.openSchematicViewer"),
        this.actionItem("Retry Ingest", "smartschy.retryIngest"),
        this.actionItem("Retry Last Chat Request", "smartschy.retryAsk"),
      ];
    }

    if (element.label === "Core Inputs") {
      return [
        this.fileItem("DSN", this.state.resources.dsnPath),
        this.fileItem("BOM", this.state.resources.bomCsvPath),
        this.fileItem("Schematic", this.state.resources.schematicPdfPath),
      ];
    }

    if (String(element.label).startsWith("Datasheets")) {
      if (this.state.resources.datasheetMappings.length === 0) {
        const empty = new ResourceTreeItem("No mapped datasheets", vscode.TreeItemCollapsibleState.None);
        empty.description = "Add datasheets";
        return [empty];
      }
      return this.state.resources.datasheetMappings.map((mapping) => {
        const item = new ResourceTreeItem(mapping.partNumber, vscode.TreeItemCollapsibleState.None);
        item.description = mapping.manufacturer || "manufacturer unspecified";
        item.tooltip = `Mapped to ${mapping.mappedPath}`;
        item.resourceUri = vscode.Uri.file(mapping.mappedPath);
        return item;
      });
    }

    if (String(element.label).startsWith("Parts Without Datasheets")) {
      if (this.state.resources.unmatchedParts.length === 0) {
        const empty = new ResourceTreeItem("No unmatched BOM parts", vscode.TreeItemCollapsibleState.None);
        empty.description = "All mapped";
        return [empty];
      }
      const groups = this.groupUnmatchedParts();
      return [
        this.unmatchedGroupItem("ICs", groups.ics.length, "unmatched-subgroup-ics"),
        this.unmatchedGroupItem("Passives", groups.passives.length, "unmatched-subgroup-passives"),
        this.unmatchedGroupItem("Other", groups.other.length, "unmatched-subgroup-other"),
      ].filter((item) => !String(item.label).endsWith("(0)"));
    }

    if (element.contextValue === "unmatched-subgroup-ics") {
      return this.groupUnmatchedParts().ics.map((part) => this.unmatchedPartItem(part));
    }
    if (element.contextValue === "unmatched-subgroup-passives") {
      return this.groupUnmatchedParts().passives.map((part) => this.unmatchedPartItem(part));
    }
    if (element.contextValue === "unmatched-subgroup-other") {
      return this.groupUnmatchedParts().other.map((part) => this.unmatchedPartItem(part));
    }

    if (String(element.label).startsWith("Responses")) {
      const responses = this.listResponseFiles();
      if (responses.length === 0) {
        const empty = new ResourceTreeItem("No responses yet", vscode.TreeItemCollapsibleState.None);
        empty.description = "Run chat after ingest";
        return [empty];
      }
      return responses.map((filePath) => {
        const item = new ResourceTreeItem(path.basename(filePath), vscode.TreeItemCollapsibleState.None);
        item.description = this.formatResponseTimestamp(path.basename(filePath));
        item.tooltip = filePath;
        item.resourceUri = vscode.Uri.file(filePath);
        item.command = {
          command: "vscode.open",
          title: "Open response markdown",
          arguments: [vscode.Uri.file(filePath)],
        };
        return item;
      });
    }

    return [];
  }

  private actionItem(label: string, command: string): vscode.TreeItem {
    const item = new ResourceTreeItem(label, vscode.TreeItemCollapsibleState.None);
    item.command = { command, title: label };
    item.contextValue = "action-item";
    return item;
  }

  private unmatchedPartItem(part: UnmatchedPart): vscode.TreeItem {
    const item = new ResourceTreeItem(`${part.refdes} ${part.partNumber}`, vscode.TreeItemCollapsibleState.None);
    item.description = part.manufacturer || "manufacturer unspecified";
    item.tooltip = "Click to choose a PDF datasheet";
    item.command = {
      command: "smartschy.mapUnmatchedPart",
      title: "Map unmatched part",
      arguments: [part],
    };
    return item;
  }

  private unmatchedGroupItem(
    label: string,
    count: number,
    contextValue: "unmatched-subgroup-ics" | "unmatched-subgroup-passives" | "unmatched-subgroup-other"
  ): vscode.TreeItem {
    const item = new ResourceTreeItem(`${label} (${count})`, vscode.TreeItemCollapsibleState.Expanded);
    item.contextValue = contextValue;
    return item;
  }

  private groupUnmatchedParts(): {
    ics: UnmatchedPart[];
    passives: UnmatchedPart[];
    other: UnmatchedPart[];
  } {
    const sorted = [...this.state.resources.unmatchedParts].sort((a, b) => {
      const ref = a.refdes.localeCompare(b.refdes);
      if (ref !== 0) {
        return ref;
      }
      return a.partNumber.localeCompare(b.partNumber);
    });
    return {
      ics: sorted.filter((part) => part.category === "ic"),
      passives: sorted.filter((part) => part.category === "passive"),
      other: sorted.filter((part) => part.category === "other"),
    };
  }

  private fileItem(label: string, filePath?: string): vscode.TreeItem {
    const item = new ResourceTreeItem(label, vscode.TreeItemCollapsibleState.None);
    if (filePath) {
      item.description = path.basename(filePath);
      item.tooltip = filePath;
      item.resourceUri = vscode.Uri.file(filePath);
      item.command = {
        command: "vscode.open",
        title: "Open file",
        arguments: [vscode.Uri.file(filePath)],
      };
    } else {
      item.description = "missing";
    }
    return item;
  }

  private listResponseFiles(): string[] {
    const dir = path.join(this.workspaceRoot, "derived", "qa", "responses");
    if (!fs.existsSync(dir)) {
      return [];
    }
    return fs
      .readdirSync(dir)
      .filter((name) => name.toLowerCase().endsWith(".md"))
      .sort((a, b) => b.localeCompare(a))
      .map((name) => path.join(dir, name));
  }

  private formatResponseTimestamp(filename: string): string {
    const match = filename.match(/response_(\d{8})_(\d{6})\.md$/i);
    if (!match) {
      return "markdown";
    }
    const date = match[1];
    const time = match[2];
    return `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)} ${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6)}`;
  }
}

