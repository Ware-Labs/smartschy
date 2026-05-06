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
exports.initialWorkflowState = void 0;
exports.getWorkspaceRoot = getWorkspaceRoot;
exports.getDerivedDir = getDerivedDir;
exports.getQaStateDir = getQaStateDir;
exports.nextStatusFromResources = nextStatusFromResources;
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
exports.initialWorkflowState = {
    status: "EMPTY",
    resources: {
        datasheetMappings: [],
        unmatchedParts: [],
    },
};
function getWorkspaceRoot() {
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder) {
        throw new Error("Open a workspace folder before using the PCB QA extension.");
    }
    return folder.uri.fsPath;
}
function getDerivedDir(workspaceRoot) {
    return path.join(workspaceRoot, "derived");
}
function getQaStateDir(workspaceRoot) {
    return path.join(workspaceRoot, ".smartschy");
}
function nextStatusFromResources(resources) {
    const hasCoreFiles = Boolean(resources.dsnPath && resources.bomCsvPath && resources.schematicPdfPath);
    if (!hasCoreFiles) {
        return "EMPTY";
    }
    if ((resources.datasheetMappings?.length ?? 0) === 0) {
        return "FILES_SELECTED";
    }
    return "READY_TO_INGEST";
}
//# sourceMappingURL=workflowState.js.map