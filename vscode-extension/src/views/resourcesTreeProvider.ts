import * as path from "path";
import * as vscode from "vscode";
import { WorkflowState } from "../state/workflowState";

class ResourceTreeItem extends vscode.TreeItem {
  public constructor(label: string, collapsibleState: vscode.TreeItemCollapsibleState) {
    super(label, collapsibleState);
  }
}

export class ResourcesTreeProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly emitter = new vscode.EventEmitter<void>();
  private state: WorkflowState;

  public readonly onDidChangeTreeData = this.emitter.event;

  public constructor(initialState: WorkflowState) {
    this.state = initialState;
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

      const core = new ResourceTreeItem("Core Inputs", vscode.TreeItemCollapsibleState.Expanded);
      core.contextValue = "core-group";
      const datasheets = new ResourceTreeItem(
        `Datasheets (${this.state.resources.datasheetMappings.length})`,
        vscode.TreeItemCollapsibleState.Expanded
      );
      datasheets.contextValue = "datasheets-group";
      return [summary, core, datasheets];
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

    return [];
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
}

