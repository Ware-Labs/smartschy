import * as path from "path";
import * as vscode from "vscode";

export type WorkflowStatus =
  | "EMPTY"
  | "FILES_SELECTED"
  | "DATASHEETS_MAPPED"
  | "READY_TO_INGEST"
  | "INGESTING"
  | "READY_FOR_CHAT"
  | "ERROR";

export interface DatasheetMapping {
  manufacturer: string;
  partNumber: string;
  sourcePath: string;
  mappedPath: string;
}

export interface UnmatchedPart {
  manufacturer: string;
  partNumber: string;
  refdes: string;
  category: "passive" | "ic" | "other";
}

export interface WorkflowResources {
  dsnPath?: string;
  bomCsvPath?: string;
  schematicPdfPath?: string;
  resourcesDir?: string;
  datasheetMappings: DatasheetMapping[];
  unmatchedParts: UnmatchedPart[];
}

export interface WorkflowState {
  status: WorkflowStatus;
  resources: WorkflowResources;
  ingestSummaryPath?: string;
  lastError?: string;
  lastIngestSignature?: string;
  lastIngestAt?: string;
}

export const initialWorkflowState: WorkflowState = {
  status: "EMPTY",
  resources: {
    datasheetMappings: [],
    unmatchedParts: [],
  },
};

export function getWorkspaceRoot(): string {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("Open a workspace folder before using the PCB QA extension.");
  }
  return folder.uri.fsPath;
}

export function getDerivedDir(workspaceRoot: string): string {
  return path.join(workspaceRoot, "derived");
}

export function getQaStateDir(workspaceRoot: string): string {
  return path.join(workspaceRoot, ".smartschy");
}

export function nextStatusFromResources(resources: WorkflowResources): WorkflowStatus {
  const hasCoreFiles = Boolean(resources.dsnPath && resources.bomCsvPath && resources.schematicPdfPath);
  if (!hasCoreFiles) {
    return "EMPTY";
  }
  if ((resources.datasheetMappings?.length ?? 0) === 0) {
    return "FILES_SELECTED";
  }
  return "READY_TO_INGEST";
}

