"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.ResourcesTreeProvider = void 0;
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
class ResourceTreeItem extends vscode.TreeItem {
    constructor(label, collapsibleState) {
        super(label, collapsibleState);
    }
}
class ResourcesTreeProvider {
    constructor(initialState) {
        this.emitter = new vscode.EventEmitter();
        this.onDidChangeTreeData = this.emitter.event;
        this.state = initialState;
    }
    updateState(state) {
        this.state = state;
        this.emitter.fire();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren(element) {
        if (!element) {
            const summary = new ResourceTreeItem(`Workflow: ${this.state.status}`, vscode.TreeItemCollapsibleState.None);
            summary.description = this.state.lastError ? "error" : "ok";
            summary.contextValue = "workflow";
            const core = new ResourceTreeItem("Core Inputs", vscode.TreeItemCollapsibleState.Expanded);
            core.contextValue = "core-group";
            const datasheets = new ResourceTreeItem(`Datasheets (${this.state.resources.datasheetMappings.length})`, vscode.TreeItemCollapsibleState.Expanded);
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
    fileItem(label, filePath) {
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
        }
        else {
            item.description = "missing";
        }
        return item;
    }
}
exports.ResourcesTreeProvider = ResourcesTreeProvider;
//# sourceMappingURL=resourcesTreeProvider.js.map